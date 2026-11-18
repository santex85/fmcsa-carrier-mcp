"""Live smoke test: calls every tool against data.transportation.gov. Run: .venv/bin/python tests/live_test.py"""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import server  # noqa: E402
from soda import Soda  # noqa: E402


def show(name, r, ok):
    s = json.dumps(r, ensure_ascii=False)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(s)} bytes | {s[:700]}\n")
    return bool(ok)


async def timed(name, coro):
    t = time.time()
    r = await coro
    return f"{name} ({time.time() - t:.1f}s)", r


async def main():
    res = []
    tools = await server.mcp.list_tools()
    res.append(show("list_tools", [t.name for t in tools], len(tools) == 4))

    n, r = await timed("lookup_carrier usdot=80806", server.lookup_carrier(usdot="80806"))
    res.append(show(n, r, r.get("found") and r["carrier"]["legalName"].startswith("J B HUNT") and r["dataFreshness"][0]["rowsUpdatedAt"]))
    n, r = await timed("lookup_carrier mc=MC-136818", server.lookup_carrier(mc_number="MC-136818"))
    res.append(show(n, r, r.get("found") and r["carrier"]["usdot"] == "54283"))
    n, r = await timed("lookup_carrier missing", server.lookup_carrier(usdot="99999999"))
    res.append(show(n, r, r.get("found") is False))
    n, r = await timed("lookup_carrier bad input", server.lookup_carrier(usdot="abc"))
    res.append(show(n, r, "error" in r))

    n, r = await timed("search_carriers swift/AZ", server.search_carriers("swift transportation", state="AZ", limit=5))
    res.append(show(n, r, r.get("returned", 0) > 0 and r["carriers"][0]["usdot"] == "54283"))
    n, r = await timed("search_carriers for-hire TX", server.search_carriers("express", state="TX", for_hire_only=True, limit=3))
    res.append(show(n, r, r.get("returned") == 3))

    n, r = await timed("check_authority 80806", server.check_authority(usdot="80806"))
    res.append(show(n, {k: r.get(k) for k in ("usdot", "activeAuthorityTypes", "firstAuthorityGrantDate", "outOfServiceInEffect", "safety")}
                    | {"insuranceOnFile": r.get("insuranceOnFile", [])[:1]},
                    r.get("activeAuthorityTypes") and r.get("insuranceOnFile") and r["safety"]["inspections"].get("total", 0) > 0))

    # a carrier whose authority was granted in the last 30 days (taken live from the Motus authority history)
    s = Soda()
    try:
        fresh = await s.rows("yu5v-wbh6", where="upper(reason)='GRANTED'", order="status_change_date DESC",
                             select="usdot_number,status_change_date", limit=1)
    finally:
        await s.close()
    new_dot = fresh[0]["usdot_number"]
    n, r = await timed(f"check_authority new {new_dot}", server.check_authority(usdot=new_dot))
    res.append(show(n, {k: r.get(k) for k in ("usdot", "census", "authorities", "latestAuthorityGrantDate")}, r.get("latestAuthorityGrantDate")))

    n, r = await timed(f"red_flags new {new_dot}", server.red_flags(usdot=new_dot))
    res.append(show(n, {k: r.get(k) for k in ("usdot", "legalName", "riskLevel", "score", "flags")},
                    any(f["code"] == "NEW_AUTHORITY" for f in r.get("flags", []))))
    n, r = await timed("red_flags 80806", server.red_flags(usdot="80806"))
    res.append(show(n, {k: r.get(k) for k in ("usdot", "riskLevel", "score", "flags")}, r.get("riskLevel") in ("low", "medium")))
    n, r = await timed("red_flags not found", server.red_flags(usdot="99999999"))
    res.append(show(n, r, r.get("riskLevel") == "high" and r["flags"][0]["code"] == "NOT_FOUND"))
    print(f"{sum(res)}/{len(res)} passed")


asyncio.run(main())
