"""Minimal async client for FMCSA open datasets on data.transportation.gov (Socrata SODA API, keyless)."""
import asyncio
import time
from datetime import datetime, timezone

import httpx

UA = "fmcsa-carrier-mcp/1.0 (MCP server; reads FMCSA open data on data.transportation.gov; python-httpx)"
BASE = "https://data.transportation.gov"
HTTP_TIMEOUT = 45  # seconds per HTTP request

# Dataset ids (all FMCSA, published on data.transportation.gov, refreshed daily).
DS = {
    "census": "az4n-8mr2",          # Company Census File (USDOT registrations, MCS-150 data)
    "carrier": "inys-ebih",         # Motus Carrier - All With History (operating authority per docket/type)
    "auth_hist": "yu5v-wbh6",       # Motus AuthHist - All With History (authority status changes)
    "insur": "c5y8-a4uz",           # Motus Insur - All With History (active/pending insurance filings)
    "ins_hist": "3uet-3z4i",        # Motus InsHist - All With History (cancelled/replaced filings)
    "revoke": "wb4f-neki",          # Motus RevokeSuspend - All With History (revocation/suspension orders)
    "oos": "p2mt-9ige",             # Out Of Service Orders
    "inspections": "fx4q-ay7w",     # Vehicle Inspection File
    "crashes": "aayw-vxb3",         # Crash File
    "legacy_auth_hist": "9mw4-x3tu",  # legacy L&I AuthHist (pre-Motus; no new actions after ~May 2026)
}
DS_NAMES = {
    "az4n-8mr2": "Company Census File", "inys-ebih": "Motus Carrier - All With History",
    "yu5v-wbh6": "Motus AuthHist - All With History", "c5y8-a4uz": "Motus Insur - All With History",
    "3uet-3z4i": "Motus InsHist - All With History", "wb4f-neki": "Motus RevokeSuspend - All With History",
    "p2mt-9ige": "Out Of Service Orders", "fx4q-ay7w": "Vehicle Inspection File", "aayw-vxb3": "Crash File",
    "9mw4-x3tu": "AuthHist - All With History (legacy L&I)",
}


class SodaError(Exception):
    pass


def q(v: str) -> str:
    """Quote a SoQL string literal."""
    return "'" + str(v).replace("'", "''") + "'"


class Soda:
    _fresh: dict[str, tuple[float, str | None]] = {}  # dataset id -> (fetched_at, rowsUpdatedAt ISO)

    def __init__(self):
        self.http = httpx.AsyncClient(base_url=BASE, headers={"User-Agent": UA, "Accept": "application/json"},
                                      timeout=HTTP_TIMEOUT, follow_redirects=True)

    async def close(self):
        await self.http.aclose()

    async def rows(self, ds: str, where: str | None = None, select: str | None = None, order: str | None = None,
                   group: str | None = None, limit: int = 100, **eq) -> list[dict]:
        params = {"$limit": str(limit)}
        if where:
            params["$where"] = where
        if select:
            params["$select"] = select
        if order:
            params["$order"] = order
        if group:
            params["$group"] = group
        params.update({k: str(v) for k, v in eq.items()})
        for attempt in range(3):
            try:
                r = await self.http.get(f"/resource/{ds}.json", params=params)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise SodaError(f"{DS_NAMES.get(ds, ds)}: network error {type(e).__name__}") from e
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                msg = r.text[:300]
                try:
                    msg = r.json().get("message", msg)[:300]
                except Exception:
                    pass
                raise SodaError(f"{DS_NAMES.get(ds, ds)}: HTTP {r.status_code}: {msg}")
            return r.json()
        return []

    async def updated_at(self, ds: str) -> str | None:
        """Dataset 'rowsUpdatedAt' (UTC ISO), cached for an hour."""
        hit = self._fresh.get(ds)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        val = None
        try:
            r = await self.http.get(f"/api/views/{ds}.json")
            if r.status_code == 200:
                ts = r.json().get("rowsUpdatedAt")
                if ts:
                    val = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (httpx.HTTPError, ValueError):
            pass
        self._fresh[ds] = (time.time(), val)
        return val

    async def freshness(self, ids: list[str]) -> list[dict]:
        vals = await asyncio.gather(*(self.updated_at(i) for i in ids))
        return [{"dataset": DS_NAMES.get(i, i), "id": i, "url": f"{BASE}/d/{i}", "rowsUpdatedAt": v}
                for i, v in zip(ids, vals)]
