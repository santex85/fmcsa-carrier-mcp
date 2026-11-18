"""FMCSA Carrier Vetting MCP server.

US motor-carrier / broker vetting from FMCSA open data published on data.transportation.gov (Socrata SODA API,
keyless). Read-only. Returns only what FMCSA publishes; company officer names, e-mail addresses and cell phones
present in the census file are deliberately NOT returned. Risk signals are heuristics, not a verdict.
"""
import asyncio
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

sys.path.insert(0, os.path.dirname(__file__))
from soda import DS, Soda, SodaError, q  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("fmcsa-carrier", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))

CALL_TIMEOUT = 90       # seconds per tool call
MAX_SEARCH = 100        # rows per search_carriers call
MAX_HISTORY = 50        # authority-history / order rows returned
MAX_SHARED_LIST = 10    # other USDOTs listed when a phone/address is shared
SAFETY_WINDOW_DAYS = 730

DISCLAIMER = ("Source: FMCSA open data on data.transportation.gov (public, refreshed daily; may lag FMCSA SAFER/L&I "
              "web pages by a day or more). Informational only, not a substitute for checking FMCSA directly or for "
              "a carrier-monitoring service.")

STATUS = {"A": "active", "I": "inactive", "P": "pending"}
OPERATION = {"A": "interstate", "B": "intrastate hazmat", "C": "intrastate non-hazmat"}
RATING = {"S": "Satisfactory", "C": "Conditional", "U": "Unsatisfactory", "N": "Not rated"}
CARGO = {
    "genfreight": "General Freight", "household": "Household Goods", "metalsheet": "Metal: sheets, coils, rolls",
    "motoveh": "Motor Vehicles", "drivetow": "Drive/Tow away", "logpole": "Logs, Poles, Beams, Lumber",
    "bldgmat": "Building Materials", "mobilehome": "Mobile Homes", "machlrg": "Machinery, Large Objects",
    "produce": "Fresh Produce", "liqgas": "Liquids/Gases", "intermodal": "Intermodal Cont.",
    "passengers": "Passengers", "oilfield": "Oilfield Equipment", "livestock": "Livestock",
    "grainfeed": "Grain, Feed, Hay", "coalcoke": "Coal/Coke", "meat": "Meat", "garbage": "Garbage/Refuse",
    "usmail": "US Mail", "chem": "Chemicals", "drybulk": "Commodities Dry Bulk", "coldfood": "Refrigerated Food",
    "beverages": "Beverages", "paperprod": "Paper Products", "utility": "Utilities",
    "farmsupp": "Agricultural/Farm Supplies", "construct": "Construction", "waterwell": "Water Well",
}
INS_FORM = {"91": "BIPD (liability)", "91X": "BIPD (liability)", "82": "BIPD (state)", "34": "Cargo",
            "83": "Cargo", "84": "Surety bond (broker/FF)", "85": "Trust fund (broker/FF)"}
INS_CLASS = {"P": "primary", "E": "excess"}


# ---------- helpers ----------

def _iso(d: str | None) -> str | None:
    """'20250707' / '07/25/1980' / '2022-07-09' -> '2025-07-07'."""
    if not d:
        return None
    d = d.strip()[:10]
    for fmt in ("%Y%m%d", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d[:8] if fmt == "%Y%m%d" else d, fmt).date().isoformat()
        except ValueError:
            pass
    return d


def _days_ago(iso: str | None) -> int | None:
    try:
        return (date.today() - date.fromisoformat(iso)).days if iso else None
    except ValueError:
        return None


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _dot(v) -> str:
    s = re.sub(r"\D", "", str(v or ""))
    if not s or len(s) > 8:
        raise ValueError(f"invalid USDOT number {v!r} (expected 1-8 digits)")
    return str(int(s))


def _docket(v) -> tuple[str, str]:
    """'MC-135797' / 'mc 135797' / '135797' -> ('MC', '135797')."""
    m = re.fullmatch(r"\s*(MC|FF|MX)?[\s\-#]*0*(\d{1,8})\s*", str(v or ""), re.I)
    if not m:
        raise ValueError(f"invalid docket number {v!r} (expected e.g. 'MC-123456', 'FF-1234' or '123456')")
    return (m.group(1) or "MC").upper(), m.group(2)


def _phone(p: str | None) -> str | None:
    d = re.sub(r"\D", "", p or "")
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (p or None)


def _addr(street, city, state, zip_, country) -> str | None:
    parts = [p for p in (street, city, " ".join(x for x in (state, (zip_ or "")[:5]) if x), country) if p]
    return ", ".join(parts) or None


def _err(e: Exception) -> dict:
    return {"error": str(e)}


async def _run(fn):
    soda = Soda()
    try:
        return await asyncio.wait_for(fn(soda), timeout=CALL_TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": f"timed out after {CALL_TIMEOUT}s (data.transportation.gov slow); try again"}
    except (SodaError, ValueError) as e:
        return _err(e)
    finally:
        await soda.close()


def _census_profile(r: dict) -> dict:
    cargo = [CARGO[k[5:]] for k, v in r.items() if k.startswith("crgo_") and k[5:] in CARGO and v == "X"]
    if r.get("crgo_cargoothr") == "X":
        cargo.append("Other: " + (r.get("crgo_cargoothr_desc") or "").strip())
    dockets = [f"{r.get(f'docket{i}prefix')}-{r.get(f'docket{i}')}" for i in (1, 2, 3) if r.get(f"docket{i}")]
    rating = r.get("safety_rating")
    return {
        "usdot": r.get("dot_number"),
        "legalName": r.get("legal_name"),
        "dbaName": r.get("dba_name"),
        "usdotStatus": STATUS.get(r.get("status_code"), r.get("status_code")),
        "entityClass": r.get("classdef"),
        "carrierOperation": OPERATION.get(r.get("carrier_operation"), r.get("carrier_operation")),
        "entityTypesRaw": r.get("carship"),
        "dockets": dockets,
        "businessType": r.get("business_org_desc"),
        "physicalAddress": _addr(r.get("phy_street"), r.get("phy_city"), r.get("phy_state"), r.get("phy_zip"),
                                 r.get("phy_country")),
        "mailingAddress": _addr(r.get("carrier_mailing_street"), r.get("carrier_mailing_city"),
                                r.get("carrier_mailing_state"), r.get("carrier_mailing_zip"),
                                r.get("carrier_mailing_country")),
        "phone": _phone(r.get("phone")),
        "powerUnits": _int(r.get("power_units")),
        "trucks": _int(r.get("truck_units")),
        "buses": _int(r.get("bus_units")),
        "drivers": _int(r.get("total_drivers")),
        "cdlDrivers": _int(r.get("total_cdl")),
        "hazmat": r.get("hm_ind") == "Y",
        "cargo": cargo,
        "mcs150Date": _iso(r.get("mcs150_date")),
        "mcs150Mileage": _int(r.get("mcs150_mileage")),
        "mcs150MileageYear": _int(r.get("mcs150_mileage_year")),
        "usdotAddedDate": _iso(r.get("add_date")),
        "safetyRating": RATING.get(rating, rating) if rating else "Not rated",
        "safetyRatingDate": _iso(r.get("safety_rating_date")),
        "priorRevokeFlag": r.get("prior_revoke_flag"),
        "priorRevokedUsdot": r.get("prior_revoke_dot_number"),
        "physicalAddressUndeliverable": r.get("undeliv_phy") == "Y" or None,
        "dunsNumber": r.get("dun_bradstreet_no"),
        "_phoneRaw": re.sub(r"\D", "", r.get("phone") or ""),
        "_phyStreet": r.get("phy_street"), "_phyZip": (r.get("phy_zip") or "")[:5],
    }


def _public(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}


async def _census_by_dot(soda: Soda, dot: str) -> dict | None:
    rows = await soda.rows(DS["census"], dot_number=dot, limit=1)
    return rows[0] if rows else None


async def _resolve(soda: Soda, usdot: str | None, mc_number: str | None) -> tuple[str | None, dict | None, str]:
    """Return (usdot, census_row, how)."""
    if usdot:
        dot = _dot(usdot)
        return dot, await _census_by_dot(soda, dot), "usdot"
    if not mc_number:
        raise ValueError("provide usdot or mc_number")
    pre, num = _docket(mc_number)
    where = " OR ".join(f"(docket{i}prefix={q(pre)} AND docket{i}={q(num)})" for i in (1, 2, 3))
    rows = await soda.rows(DS["census"], where=where, limit=5)
    if rows:
        return rows[0]["dot_number"], rows[0], f"{pre}-{num} via census docket fields"
    # Motus-era dockets are stored as e.g. 'MC1589648' in the Motus carrier file
    rows = await soda.rows(DS["carrier"], where=f"docket_number in ({q(pre + num)}, {q(pre + '-' + num)})",
                           select="usdot_number", limit=1)
    if rows and rows[0].get("usdot_number"):
        dot = str(int(rows[0]["usdot_number"]))
        return dot, await _census_by_dot(soda, dot), f"{pre}-{num} via Motus carrier file"
    return None, None, f"{pre}-{num}"


def _dedupe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        k = tuple(sorted(r.items()))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


async def _authority(soda: Soda, dot: str) -> dict:
    """Operating authority, insurance, orders and OOS from the Motus L&I datasets + OOS file."""
    pad = dot.zfill(8)
    carrier, hist, insur, ins_hist, orders, oos, legacy = await asyncio.gather(
        soda.rows(DS["carrier"], usdot_number=dot, limit=60),
        soda.rows(DS["auth_hist"], usdot_number=dot, order="status_change_date DESC", limit=200),
        soda.rows(DS["insur"], usdot_number=dot, limit=200),
        soda.rows(DS["ins_hist"], usdot_number=dot, order="cancl_effective_date DESC", limit=200),
        soda.rows(DS["revoke"], usdot_number=dot, order="order1_serve_date DESC", limit=100),
        soda.rows(DS["oos"], dot_number=dot, limit=50),
        soda.rows(DS["legacy_auth_hist"], dot_number=pad, select="docket_number,mod_col_1,original_action_desc,"
                  "orig_served_date,disp_action_desc,disp_served_date", limit=200),
    )
    carrier, hist, insur, ins_hist, orders = map(_dedupe, (carrier, hist, insur, ins_hist, orders))

    # Current authorities: keep one row per (docket, type), Active > Pending > others.
    rank = {"Active": 0, "Pending": 1}
    best: dict[tuple, dict] = {}
    for r in carrier:
        k = (r.get("docket_number"), r.get("op_auth_type"))
        if k not in best or rank.get(r.get("op_auth_status"), 9) < rank.get(best[k].get("op_auth_status"), 9):
            best[k] = r
    authorities = [{
        "docket": r.get("docket_number"), "type": r.get("op_auth_type"), "status": r.get("op_auth_status"),
        "bipdRequired": _int(r.get("min_cov_amount")), "bipdOnFile": _int(r.get("bipd_file")),
        "cargoRequired": r.get("cargo_req") == "Y", "cargoOnFile": r.get("cargo_file") == "Y",
        "bondRequired": r.get("bond_req") == "Y", "bondOnFile": r.get("bond_file") == "Y",
    } for r in best.values()]
    authorities.sort(key=lambda a: (rank.get(a["status"], 9), a["type"] or ""))
    l_and_i_name = next((r.get("legal_name") for r in carrier if r.get("legal_name")), None)
    l_and_i_addr = next((_addr(r.get("bus_street_po"), r.get("bus_city"), r.get("bus_state_code"),
                               r.get("bus_zip_code"), r.get("bus_ctry_code")) for r in carrier if r.get("bus_city")), None)
    l_and_i_phone = next((_phone(r.get("bus_telno")) for r in carrier if r.get("bus_telno")), None)

    # Grant dates: Motus 'GRANTED'/'Granted' rows; legacy 'GRANTED' rows (pre-Motus history).
    grants = []
    for r in hist:
        if (r.get("reason") or "").upper() == "GRANTED":
            grants.append({"type": r.get("op_auth_type"), "docket": r.get("docket_number"),
                           "date": _iso(r.get("status_change_date")), "source": "Motus"})
    for r in legacy:
        if (r.get("original_action_desc") or "").upper() == "GRANTED":
            grants.append({"type": r.get("mod_col_1"), "docket": r.get("docket_number"),
                           "date": _iso(r.get("orig_served_date")), "source": "legacy L&I"})
    grant_dates = sorted(g["date"] for g in grants if g["date"])

    history = [{"date": _iso(r.get("status_change_date")), "docket": r.get("docket_number"),
                "type": r.get("op_auth_type"), "status": r.get("op_auth_status"), "reason": r.get("reason")}
               for r in hist[:MAX_HISTORY]]

    today = date.today().isoformat()
    policies = []
    for r in insur:
        form = (r.get("ins_form_code") or "").replace("BMC-", "").upper()
        policies.append({
            "docket": r.get("docket_number"), "form": r.get("ins_form_code"), "coverage": INS_FORM.get(form, form),
            "class": INS_CLASS.get(r.get("ins_class_code"), r.get("ins_class_code")),
            "insurer": r.get("insurance_company_name"), "policyNo": r.get("policy_no"),
            "maxCoverage": _int(r.get("max_cov_amount")), "underlyingLimit": _int(r.get("underl_lim_amount")),
            "effectiveDate": _iso(r.get("effective_date")), "filedDate": _iso(r.get("trans_date")),
        })
    cancellations = []
    for r in ins_hist:
        c = _iso(r.get("cancl_effective_date"))
        if c:
            cancellations.append({
                "docket": r.get("docket_number"), "form": r.get("ins_form_code"), "coverage": r.get("ins_type_desc"),
                "insurer": r.get("insurance_company_name"), "policyNo": r.get("policy_no"),
                "effectiveDate": _iso(r.get("effective_date")), "cancelEffectiveDate": c,
                "reason": r.get("filing_status_reason"), "pending": c > today,
            })
    cancellations.sort(key=lambda c: c["cancelEffectiveDate"], reverse=True)

    order_list = [{
        "docket": r.get("docket_number"), "type": r.get("op_auth_type"), "order": r.get("order1_type_desc"),
        "servedDate": _iso(r.get("order1_serve_date")), "effectiveDate": _iso(r.get("order1_effective_date")),
        "pending": (_iso(r.get("order1_effective_date")) or "") > today,
    } for r in orders[:MAX_HISTORY]]

    oos_list = [{"oosDate": _iso(r.get("oos_date")), "reason": r.get("oos_reason"), "status": r.get("status"),
                 "rescindDate": _iso(r.get("rescind_date")), "inEffect": not r.get("rescind_date")} for r in oos]
    oos_list.sort(key=lambda o: o["oosDate"] or "", reverse=True)

    return {
        "authorities": authorities,
        "activeAuthorityTypes": sorted({a["type"] for a in authorities if a["status"] == "Active"}),
        "lAndIRecord": {"legalName": l_and_i_name, "businessAddress": l_and_i_addr, "businessPhone": l_and_i_phone},
        "firstAuthorityGrantDate": grant_dates[0] if grant_dates else None,
        "latestAuthorityGrantDate": grant_dates[-1] if grant_dates else None,
        "authorityGrants": sorted(grants, key=lambda g: g["date"] or "", reverse=True)[:20],
        "authorityHistory": history,
        "insuranceOnFile": policies[:50],
        "insuranceCancellations": cancellations[:25],
        "revocationSuspensionOrders": order_list,
        "outOfServiceOrders": oos_list,
        "outOfServiceInEffect": any(o["inEffect"] for o in oos_list),
    }


async def _safety(soda: Soda, dot: str) -> dict:
    since = (date.today() - timedelta(days=SAFETY_WINDOW_DAYS)).strftime("%Y%m%d")
    insp_sel = ("count(*) as n, sum(oos_total::number) as oos, sum(driver_oos_total::number) as drv_oos, "
                "sum(vehicle_oos_total::number) as veh_oos, sum(viol_total::number) as viol, "
                "sum(hazmat_oos_total::number) as hm_oos")
    crash_sel = ("count(*) as n, sum(fatalities::number) as fatal, sum(injuries::number) as inj, "
                 "sum(case(tow_away='Y', 1, true, 0)) as tow")
    insp, crash = await asyncio.gather(
        soda.rows(DS["inspections"], where=f"dot_number={q(dot)} AND insp_date >= {q(since)}", select=insp_sel, limit=1),
        soda.rows(DS["crashes"], where=f"dot_number={q(dot)} AND report_date >= {q(since)}", select=crash_sel, limit=1),
        return_exceptions=True,
    )
    out = {"windowStart": _iso(since), "windowDays": SAFETY_WINDOW_DAYS}
    if isinstance(insp, Exception):
        out["inspections"] = {"error": str(insp)}
    else:
        i = insp[0] if insp else {}
        n = _int(i.get("n")) or 0
        out["inspections"] = {
            "total": n, "oosViolations": _int(i.get("oos")) or 0,
            "driverOOS": _int(i.get("drv_oos")) or 0, "vehicleOOS": _int(i.get("veh_oos")) or 0,
            "hazmatOOS": _int(i.get("hm_oos")) or 0, "violations": _int(i.get("viol")) or 0,
        }
    if isinstance(crash, Exception):
        out["crashes"] = {"error": str(crash)}
    else:
        c = crash[0] if crash else {}
        out["crashes"] = {"total": _int(c.get("n")) or 0, "fatalities": _int(c.get("fatal")) or 0,
                          "injuries": _int(c.get("inj")) or 0, "towAway": _int(c.get("tow")) or 0}
    out["note"] = ("Counts from the FMCSA Vehicle Inspection File and Crash File over the window. OOS counts are "
                   "out-of-service violations, not the official SMS OOS rate; crash counts include non-preventable crashes.")
    return out


# ---------- tools ----------

@mcp.tool()
async def lookup_carrier(usdot: str | None = None, mc_number: str | None = None) -> dict:
    """Look up one US motor carrier / broker / freight forwarder by USDOT number or MC/FF/MX docket number.

    usdot: e.g. "80806". mc_number: e.g. "MC-135797", "MC135797" or "135797" (MC assumed); "FF-51075" for freight
    forwarders. Give one of the two (usdot wins if both are given).
    Returns the FMCSA Company Census (SAFER-style) profile: legal/DBA name, USDOT status (active/inactive), entity class
    (authorized for hire, private, ...), interstate/intrastate, dockets, physical and mailing address, business phone,
    power units, trucks, drivers, hazmat flag, cargo types, MCS-150 date and mileage, date added to USDOT,
    safety rating, prior-revocation flag, plus a short operating-authority summary (active authority types,
    BIPD/cargo/bond on file) from the Motus L&I file, and dataset freshness.
    Not returned on purpose: officer names, e-mail addresses, cell phones.
    """
    async def run(soda: Soda):
        dot, row, how = await _resolve(soda, usdot, mc_number)
        if not dot:
            return {"found": False, "query": how, "message": "no carrier with this docket number in FMCSA data"}
        carrier = await soda.rows(DS["carrier"], usdot_number=dot, limit=60)
        fresh = await soda.freshness([DS["census"], DS["carrier"]])
        if not row and not carrier:
            return {"found": False, "usdot": dot, "message": "USDOT number not found in the FMCSA Company Census File",
                    "dataFreshness": fresh}
        best: dict[tuple, dict] = {}
        for r in _dedupe(carrier):
            k = (r.get("docket_number"), r.get("op_auth_type"))
            if k not in best or r.get("op_auth_status") == "Active":
                best[k] = r
        summary = [{"docket": r.get("docket_number"), "type": r.get("op_auth_type"), "status": r.get("op_auth_status"),
                    "bipdOnFile": _int(r.get("bipd_file")), "cargoOnFile": r.get("cargo_file") == "Y",
                    "bondOnFile": r.get("bond_file") == "Y"} for r in best.values()]
        return {"found": True, "lookedUpBy": how,
                "carrier": _public(_census_profile(row)) if row else {"usdot": dot, "note": "not in census file"},
                "operatingAuthority": summary or "no operating-authority (MC/FF/MX) record in the Motus L&I file",
                "saferUrl": f"https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string={dot}",
                "dataFreshness": fresh, "disclaimer": DISCLAIMER}
    return await _run(run)


@mcp.tool()
async def search_carriers(name: str, state: str | None = None, city: str | None = None, active_only: bool = True,
                          for_hire_only: bool = False, limit: int = 25) -> dict:
    """Search FMCSA-registered carriers/brokers by name (legal or DBA name, case-insensitive substring).

    name: at least 3 characters, e.g. "swift transportation". state: 2-letter physical-address state, e.g. "AZ".
    city: physical city, exact match (case-insensitive), e.g. "PHOENIX". active_only: only USDOT status active
    (default true). for_hire_only: only entity class containing "AUTHORIZED FOR HIRE".
    limit: 1-100 (default 25). Returns compact rows (usdot, names, status, class, dockets, city/state, power units,
    drivers, MCS-150 date) sorted by power units, largest first. Use lookup_carrier / check_authority for detail.
    """
    async def run(soda: Soda):
        n = re.sub(r"\s+", " ", (name or "").strip().upper())
        if len(n) < 3:
            raise ValueError("name must be at least 3 characters")
        lim = max(1, min(int(limit), MAX_SEARCH))
        like = q(f"%{n}%")
        where = [f"(upper(legal_name) like {like} OR upper(dba_name) like {like})"]
        if state:
            where.append(f"phy_state={q(state.strip().upper()[:2])}")
        if city:
            where.append(f"upper(phy_city)={q(city.strip().upper())}")
        if active_only:
            where.append("status_code='A'")
        if for_hire_only:
            where.append("classdef like '%AUTHORIZED FOR HIRE%'")
        sel = ("dot_number,legal_name,dba_name,status_code,classdef,docket1prefix,docket1,docket2prefix,docket2,"
               "phy_city,phy_state,power_units,total_drivers,mcs150_date,add_date,safety_rating")
        rows = await soda.rows(DS["census"], where=" AND ".join(where), select=sel, limit=500)
        rows.sort(key=lambda r: -(_int(r.get("power_units")) or 0))
        out = [{
            "usdot": r.get("dot_number"), "legalName": r.get("legal_name"), "dbaName": r.get("dba_name"),
            "usdotStatus": STATUS.get(r.get("status_code"), r.get("status_code")), "entityClass": r.get("classdef"),
            "dockets": [f"{r.get(f'docket{i}prefix')}-{r.get(f'docket{i}')}" for i in (1, 2) if r.get(f"docket{i}")],
            "city": r.get("phy_city"), "state": r.get("phy_state"), "powerUnits": _int(r.get("power_units")),
            "drivers": _int(r.get("total_drivers")), "mcs150Date": _iso(r.get("mcs150_date")),
            "usdotAddedDate": _iso(r.get("add_date")),
            "safetyRating": RATING.get(r.get("safety_rating"), "Not rated") if r.get("safety_rating") else "Not rated",
        } for r in rows[:lim]]
        return {"query": {"name": n, "state": state, "city": city, "activeOnly": active_only,
                          "forHireOnly": for_hire_only},
                "matched": len(rows) if len(rows) < 500 else "500+ (refine with state/city)",
                "returned": len(out), "carriers": out,
                "dataFreshness": await soda.freshness([DS["census"]]), "disclaimer": DISCLAIMER}
    return await _run(run)


@mcp.tool()
async def check_authority(usdot: str | None = None, mc_number: str | None = None) -> dict:
    """Full compliance check of one carrier/broker: operating authority, insurance, orders, out-of-service, safety.

    usdot or mc_number as in lookup_carrier. Returns:
      - authorities: every docket/authority type (motor carrier of property, household goods, broker, passenger, ...)
        with status (Active/Pending/Inactive/Withdrawn), BIPD required vs on file ($), cargo and bond required/on file;
      - firstAuthorityGrantDate / latestAuthorityGrantDate and authorityGrants (Motus + legacy L&I history),
        authorityHistory (latest 50 status changes with reason, e.g. involuntary suspension for no insurance);
      - insuranceOnFile (active/pending filings: form BMC-91X/34/84/85, insurer, policy, coverage, effective date)
        and insuranceCancellations (most recent 25, flag pending=true if cancellation is still in the future);
      - revocationSuspensionOrders (pending=true if not yet effective), outOfServiceOrders, outOfServiceInEffect;
      - census basics: USDOT status, safety rating + date, power units, drivers, MCS-150 date;
      - safety: inspections (count, OOS violations, driver/vehicle/hazmat OOS) and crashes (count, fatalities,
        injuries, tow-away) over the last 24 months;
      - dataFreshness for every dataset used.
    """
    async def run(soda: Soda):
        dot, row, how = await _resolve(soda, usdot, mc_number)
        if not dot:
            return {"found": False, "query": how, "message": "no carrier with this docket number in FMCSA data"}
        auth, safety = await asyncio.gather(_authority(soda, dot), _safety(soda, dot))
        p = _census_profile(row) if row else {}
        basics = {k: p.get(k) for k in ("legalName", "dbaName", "usdotStatus", "entityClass", "carrierOperation",
                                        "safetyRating", "safetyRatingDate", "powerUnits", "drivers", "mcs150Date",
                                        "usdotAddedDate")} if row else None
        fresh = await soda.freshness([DS[k] for k in ("census", "carrier", "auth_hist", "insur", "ins_hist", "revoke",
                                                       "oos", "inspections", "crashes", "legacy_auth_hist")])
        return {"found": bool(row or auth["authorities"]), "usdot": dot, "lookedUpBy": how, "census": basics,
                **auth, "safety": safety, "dataFreshness": fresh,
                "notes": ["Motus = FMCSA's current registration system; its L&I datasets are the current source. "
                          "The legacy L&I authority history stops around May 2026 and is used only for older grant dates.",
                          "BIPD amounts are US dollars; Motus files list them in dollars."],
                "disclaimer": DISCLAIMER}
    return await _run(run)


@mcp.tool()
async def red_flags(usdot: str | None = None, mc_number: str | None = None) -> dict:
    """Heuristic fraud/risk screen for one carrier or broker (double-brokering, identity theft, chameleon carriers).

    usdot or mc_number as in lookup_carrier. Checks, each reported with severity high/medium/low and evidence:
      USDOT not found or inactive; no active operating authority; authority or USDOT newly granted (<90 / <180 days);
      pending revocation/suspension or recent involuntary suspension; out-of-service order in effect;
      no BIPD/cargo/bond on file where required; insurance cancellations (pending or in the last 12 months);
      FMCSA prior-revocation flag (possible reincarnated carrier); business phone or physical address shared with
      other USDOT numbers (lists up to 10); census vs L&I name/address mismatch; MCS-150 older than 24 months;
      Unsatisfactory/Conditional safety rating; zero power units on a for-hire carrier; high vehicle/driver OOS counts.
    Returns {riskLevel: low|medium|high, score, flags:[{severity, code, message, evidence}], checked:[...], ...}.
    Heuristics only: a flag is a reason to verify (call the carrier at the FMCSA-listed phone, check SAFER),
    not proof of fraud. Large fleets legitimately share phones/addresses with affiliates.
    """
    async def run(soda: Soda):
        dot, row, how = await _resolve(soda, usdot, mc_number)
        flags, checked = [], []

        def flag(sev, code, msg, evidence=None):
            flags.append({"severity": sev, "code": code, "message": msg, "evidence": evidence})

        if not dot or not row:
            flag("high", "NOT_FOUND", "USDOT/docket not found in the FMCSA Company Census File", {"query": how, "usdot": dot})
            return {"usdot": dot, "riskLevel": "high", "score": 100, "flags": flags,
                    "dataFreshness": await soda.freshness([DS["census"]]), "disclaimer": DISCLAIMER}
        p = _census_profile(row)
        shared_phone_q = shared_addr_q = None
        if len(p["_phoneRaw"]) == 10:
            shared_phone_q = soda.rows(DS["census"], where=f"phone={q(p['_phoneRaw'])} AND dot_number != {q(dot)}",
                                       select="dot_number,legal_name,status_code,phy_state", limit=200)
        if p["_phyStreet"] and p["_phyZip"]:
            shared_addr_q = soda.rows(DS["census"], where=f"phy_street={q(p['_phyStreet'])} AND "
                                      f"starts_with(phy_zip, {q(p['_phyZip'])}) AND dot_number != {q(dot)}",
                                      select="dot_number,legal_name,status_code,phone", limit=200)

        async def none():
            return None
        auth, safety, sh_phone, sh_addr = await asyncio.gather(
            _authority(soda, dot), _safety(soda, dot), shared_phone_q or none(), shared_addr_q or none())
        today = date.today()

        # USDOT status / age
        checked.append("usdot_status")
        if p["usdotStatus"] != "active":
            flag("high", "USDOT_INACTIVE", f"USDOT status is {p['usdotStatus']}", {"usdotStatus": p["usdotStatus"]})
        checked.append("usdot_age")
        age = _days_ago(p["usdotAddedDate"])
        if age is not None and age < 180:
            flag("high" if age < 90 else "medium", "NEW_USDOT", f"USDOT number added {age} days ago",
                 {"usdotAddedDate": p["usdotAddedDate"]})

        # Authority
        checked.append("operating_authority")
        for_hire = "AUTHORIZED FOR HIRE" in (p.get("entityClass") or "")
        active = [a for a in auth["authorities"] if a["status"] == "Active"]
        if not active:
            sev = "high" if for_hire or auth["authorities"] else "low"
            flag(sev, "NO_ACTIVE_AUTHORITY", "No active operating authority (MC/FF/MX) on file"
                 + ("" if auth["authorities"] else "; interstate for-hire hauling of regulated freight requires it"),
                 {"authorities": [{k: a[k] for k in ("docket", "type", "status")} for a in auth["authorities"]][:10]})
        checked.append("authority_age")
        g_age = _days_ago(auth["firstAuthorityGrantDate"])
        if g_age is not None and g_age < 180:
            flag("high" if g_age < 90 else "medium", "NEW_AUTHORITY",
                 f"Oldest operating-authority grant on record is only {g_age} days old",
                 {"firstAuthorityGrantDate": auth["firstAuthorityGrantDate"]})
        elif g_age is None and active:
            flag("low", "AUTHORITY_AGE_UNKNOWN", "Active authority but no grant date found in Motus or legacy history", None)

        # Insurance vs requirements
        checked.append("insurance_on_file")
        for a in active:
            missing = []
            if (a["bipdRequired"] or 0) > 0 and (a["bipdOnFile"] or 0) < (a["bipdRequired"] or 0):
                missing.append(f"BIPD on file ${a['bipdOnFile'] or 0:,} < required ${a['bipdRequired']:,}")
            if a["cargoRequired"] and not a["cargoOnFile"]:
                missing.append("cargo insurance required but not on file")
            if a["bondRequired"] and not a["bondOnFile"]:
                missing.append("surety bond/trust fund required but not on file")
            if missing:
                flag("high", "INSURANCE_MISSING", f"{a['type']} ({a['docket']}): " + "; ".join(missing), a)
        checked.append("insurance_cancellations")
        pend = [c for c in auth["insuranceCancellations"] if c["pending"]]
        recent = [c for c in auth["insuranceCancellations"] if not c["pending"]
                  and (_days_ago(c["cancelEffectiveDate"]) or 9999) <= 365 and (c.get("reason") or "").upper() == "CANCEL"]
        if pend:
            flag("high", "INSURANCE_CANCELLATION_PENDING", f"{len(pend)} insurance filing(s) have a future cancellation date", pend[:5])
        if len(recent) >= 2:
            flag("medium", "INSURANCE_CHURN", f"{len(recent)} insurance cancellations in the last 12 months", recent[:5])

        # Orders / OOS
        checked.append("revocation_suspension_orders")
        pend_o = [o for o in auth["revocationSuspensionOrders"] if o["pending"]]
        if pend_o:
            flag("high", "PENDING_REVOCATION", "Pending revocation/suspension order(s)", pend_o[:5])
        susp = [h for h in auth["authorityHistory"] if "involuntary" in (h["reason"] or "").lower()
                and (_days_ago(h["date"]) or 9999) <= 365]
        if susp:
            flag("medium", "RECENT_INVOLUNTARY_SUSPENSION", f"{len(susp)} involuntary suspension(s) in the last 12 months", susp[:5])
        checked.append("out_of_service_orders")
        if auth["outOfServiceInEffect"]:
            flag("high", "OUT_OF_SERVICE", "FMCSA out-of-service order in effect (no rescind date)",
                 [o for o in auth["outOfServiceOrders"] if o["inEffect"]][:5])
        elif auth["outOfServiceOrders"]:
            flag("low", "PAST_OUT_OF_SERVICE", f"{len(auth['outOfServiceOrders'])} past out-of-service order(s), rescinded",
                 auth["outOfServiceOrders"][:3])

        # Prior revocation (chameleon)
        checked.append("prior_revocation")
        if (p.get("priorRevokeFlag") or "").upper() == "Y" or p.get("priorRevokedUsdot"):
            flag("high", "PRIOR_REVOCATION", "FMCSA prior-revocation flag set: linked to a previously revoked USDOT (possible reincarnated carrier)",
                 {"priorRevokeFlag": p.get("priorRevokeFlag"), "priorRevokedUsdot": p.get("priorRevokedUsdot")})

        # Shared contact data
        checked.append("shared_phone")
        if sh_phone:
            n = len(sh_phone)
            n_active = sum(1 for r in sh_phone if r.get("status_code") == "A")
            big = (p["powerUnits"] or 0) >= 100
            sev = "low" if big else ("high" if n_active >= 3 else "medium")
            flag(sev, "SHARED_PHONE",
                 f"Business phone {p['phone']} is also listed on {n if n < 200 else '200+'} other USDOT record(s), "
                 f"{n_active} of them active" + (" (large fleet: affiliates commonly share a main number)" if big else ""),
                 [{"usdot": r.get("dot_number"), "legalName": r.get("legal_name"),
                   "status": STATUS.get(r.get("status_code")), "state": r.get("phy_state")} for r in sh_phone[:MAX_SHARED_LIST]])
        checked.append("shared_address")
        if sh_addr and len(sh_addr) >= 2:
            n = len(sh_addr)
            big = (p["powerUnits"] or 0) >= 100
            flag("medium" if n >= 5 and not big else "low", "SHARED_ADDRESS",
                 f"Physical address shared with {n if n < 200 else '200+'} other USDOT record(s)",
                 [{"usdot": r.get("dot_number"), "legalName": r.get("legal_name"),
                   "status": STATUS.get(r.get("status_code"))} for r in sh_addr[:MAX_SHARED_LIST]])

        # L&I vs census mismatch
        checked.append("census_vs_l_and_i")
        li = auth["lAndIRecord"]

        def norm(s):
            return re.sub(r"[^A-Z0-9]", "", (s or "").upper())
        if li.get("legalName") and norm(li["legalName"]) != norm(p["legalName"]):
            flag("low", "NAME_MISMATCH", "Legal name differs between Company Census and L&I record",
                 {"census": p["legalName"], "lAndI": li["legalName"]})
        if li.get("businessAddress") and p["physicalAddress"]:
            if norm(li["businessAddress"])[:12] != norm(p["physicalAddress"])[:12]:
                flag("low", "ADDRESS_MISMATCH", "Business address differs between Company Census and L&I record (recent move or data lag?)",
                     {"census": p["physicalAddress"], "lAndI": li["businessAddress"]})

        # MCS-150 staleness, safety rating, fleet
        checked.append("mcs150_age")
        m_age = _days_ago(p["mcs150Date"])
        if m_age is not None and m_age > 730:
            flag("medium" if m_age > 1095 else "low", "STALE_MCS150",
                 f"MCS-150 last updated {m_age} days ago (biennial update required)", {"mcs150Date": p["mcs150Date"]})
        checked.append("safety_rating")
        if p["safetyRating"] == "Unsatisfactory":
            flag("high", "UNSATISFACTORY_RATING", "Safety rating Unsatisfactory", {"date": p["safetyRatingDate"]})
        elif p["safetyRating"] == "Conditional":
            flag("medium", "CONDITIONAL_RATING", "Safety rating Conditional", {"date": p["safetyRatingDate"]})
        checked.append("fleet_size")
        if for_hire and any("Motor Carrier" in (a["type"] or "") for a in active) and not p["powerUnits"]:
            flag("medium", "NO_POWER_UNITS", "Motor-carrier authority active but 0 power units reported (possible broker posing as carrier)",
                 {"powerUnits": p["powerUnits"]})
        checked.append("inspection_oos")
        ins = safety.get("inspections") or {}
        if isinstance(ins.get("total"), int) and ins["total"] >= 5:
            rate = (ins["vehicleOOS"] + ins["driverOOS"]) / ins["total"]
            if rate >= 0.5:
                flag("medium", "HIGH_OOS", f"{ins['vehicleOOS'] + ins['driverOOS']} driver/vehicle OOS violations in "
                     f"{ins['total']} inspections over 24 months", ins)
        if for_hire and active and isinstance(ins.get("total"), int) and ins["total"] == 0 and (p["powerUnits"] or 0) >= 5:
            flag("low", "NO_INSPECTIONS", f"{p['powerUnits']} power units but no roadside inspections in 24 months", None)

        weights = {"high": 40, "medium": 15, "low": 5}
        score = min(100, sum(weights[f["severity"]] for f in flags))
        level = "high" if any(f["severity"] == "high" for f in flags) or score >= 60 else \
            "medium" if score >= 15 else "low"
        order = {"high": 0, "medium": 1, "low": 2}
        flags.sort(key=lambda f: order[f["severity"]])
        fresh = await soda.freshness([DS[k] for k in ("census", "carrier", "auth_hist", "insur", "ins_hist", "revoke",
                                                       "oos", "inspections")])
        return {"usdot": dot, "legalName": p["legalName"], "riskLevel": level, "score": score,
                "flagCount": len(flags), "flags": flags, "checked": checked,
                "summary": {"usdotStatus": p["usdotStatus"], "activeAuthorityTypes": auth["activeAuthorityTypes"],
                            "firstAuthorityGrantDate": auth["firstAuthorityGrantDate"], "powerUnits": p["powerUnits"],
                            "drivers": p["drivers"], "phone": p["phone"], "physicalAddress": p["physicalAddress"],
                            "insuranceFilings": len(auth["insuranceOnFile"])},
                "asOf": today.isoformat(), "dataFreshness": fresh,
                "disclaimer": DISCLAIMER + " Flags are heuristics, not findings of fraud."}
    return await _run(run)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
