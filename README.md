# FMCSA Carrier Vetting MCP server

An MCP server for freight brokers, shippers and 3PLs that need to vet US motor carriers and brokers before
tendering a load. It answers "who is this carrier, are they allowed to haul, are they insured, and does anything
look like fraud?" in one call. It uses FMCSA's own open data on **data.transportation.gov** (Socrata SODA API,
no key, no login).

## Tools

| Tool | What it does |
|---|---|
| `lookup_carrier` | Company profile by USDOT or MC/FF/MX number: legal/DBA name, USDOT status, entity class, dockets, address, business phone, power units, drivers, cargo types, MCS-150 date, safety rating, prior-revocation flag, and an operating-authority summary |
| `search_carriers` | Name search (legal or DBA, substring) with state, city, active-only and for-hire-only filters, up to 100 rows, largest fleets first |
| `check_authority` | Full compliance view: every authority (carrier/broker/HHG/passenger) with status, BIPD required vs on file, cargo/bond, authority grant dates and status history, insurance filings, cancellations, revocation/suspension orders, out-of-service orders, plus 24-month inspection and crash counts |
| `red_flags` | Heuristic fraud/risk screen with severity, evidence and a risk level (see below) |

### Example calls

```jsonc
// lookup_carrier
{"usdot": "80806"}
{"mc_number": "MC-136818"}          // also "MC136818", "136818", "FF-51075"

// search_carriers
{"name": "swift transportation", "state": "AZ", "active_only": true, "limit": 10}
{"name": "express", "state": "TX", "city": "HOUSTON", "for_hire_only": true}

// check_authority
{"usdot": "80806"}

// red_flags
{"mc_number": "MC-1589648"}
```

Sample `red_flags` output for a carrier whose authority was granted the day before:

```json
{"usdot": "6951245", "riskLevel": "high", "score": 80,
 "flags": [{"severity": "high", "code": "NEW_USDOT", "message": "USDOT number added 14 days ago"},
           {"severity": "high", "code": "NEW_AUTHORITY", "message": "Oldest operating-authority grant on record is only 1 days old"}]}
```

### Red-flag checks

| Code | Severity | Signal |
|---|---|---|
| `NOT_FOUND`, `USDOT_INACTIVE` | high | USDOT unknown or not active |
| `NO_ACTIVE_AUTHORITY` | high (low for private/intrastate entities) | no active MC/FF/MX authority |
| `NEW_AUTHORITY`, `NEW_USDOT` | high < 90 days, medium < 180 days | freshly created identity, typical of fraud |
| `PENDING_REVOCATION`, `RECENT_INVOLUNTARY_SUSPENSION` | high / medium | FMCSA orders not yet effective; suspensions in the last 12 months |
| `OUT_OF_SERVICE` / `PAST_OUT_OF_SERVICE` | high / low | FMCSA OOS order in effect / rescinded |
| `INSURANCE_MISSING`, `INSURANCE_CANCELLATION_PENDING`, `INSURANCE_CHURN` | high / high / medium | BIPD below required, cargo or bond missing, future cancellation, 2+ cancellations in 12 months |
| `PRIOR_REVOCATION` | high | FMCSA census prior-revocation flag (possible "chameleon" carrier) |
| `SHARED_PHONE`, `SHARED_ADDRESS` | high/medium/low | the census phone or physical address appears on other USDOTs (downgraded for fleets of 100+ trucks, whose affiliates share numbers) |
| `NAME_MISMATCH`, `ADDRESS_MISMATCH` | low | Company Census vs Motus L&I record differ |
| `STALE_MCS150` | low/medium | MCS-150 older than 2 / 3 years |
| `UNSATISFACTORY_RATING`, `CONDITIONAL_RATING` | high / medium | FMCSA safety rating |
| `NO_POWER_UNITS`, `HIGH_OOS`, `NO_INSPECTIONS` | medium / medium / low | carrier authority with 0 trucks; many OOS violations; no inspections despite 5+ trucks |

Score: high = 40, medium = 15, low = 5, capped at 100. Risk level is `high` if any high flag (or score ≥ 60),
`medium` if score ≥ 15, otherwise `low`. These are **heuristics, not a finding of fraud**. Always confirm by
calling the phone number FMCSA lists, not the one in an e-mail.

## Data sources

All are FMCSA datasets on data.transportation.gov, read live per call (nothing is downloaded or cached except
the per-dataset "last updated" timestamp, kept 1 hour):

| Dataset | Id | Used for |
|---|---|---|
| Company Census File | `az4n-8mr2` | profile, search, shared phone/address |
| Motus Carrier - All With History | `inys-ebih` | operating authority per type, BIPD/cargo/bond on file |
| Motus AuthHist - All With History | `yu5v-wbh6` | grant dates, status changes |
| Motus Insur - All With History | `c5y8-a4uz` | active/pending insurance filings |
| Motus InsHist - All With History | `3uet-3z4i` | insurance cancellations |
| Motus RevokeSuspend - All With History | `wb4f-neki` | revocation/suspension orders |
| Out Of Service Orders | `p2mt-9ige` | OOS orders |
| Vehicle Inspection File | `fx4q-ay7w` | 24-month inspection/OOS counts |
| Crash File | `aayw-vxb3` | 24-month crash counts |
| AuthHist - All With History (legacy L&I) | `9mw4-x3tu` | grant dates before the Motus migration |

**Freshness.** Every response has a `dataFreshness` list with each dataset's `rowsUpdatedAt`. FMCSA refreshes
these daily (all were updated 2026-11-17 when this was built). Data can lag the live SAFER / L&I web pages by
a day or more. **Motus** is FMCSA's current registration system. The legacy L&I files are still published, but
the legacy authority history has no new actions after about May 2026, so this server reads authority and
insurance from the Motus files and uses the legacy history only for older grant dates.

**Terms / licence.** The datasets carry no explicit licence field on data.transportation.gov. They are US
federal government works published as open data, generally public domain in the US (17 U.S.C. § 105); this was
not confirmed against a dataset-specific licence. No key is used. Socrata throttles anonymous clients, so for
heavy production traffic register a free Socrata app token (not used here). The optional FMCSA QCMobile API needs
a webKey via login.gov and is **not** used.

**Privacy.** Only what FMCSA publishes is returned, with no enrichment. The census file also contains company
officer names, e-mail addresses and cell phones. This server deliberately leaves those out and returns only the
business phone and addresses that the SAFER Company Snapshot shows.

## Limits

- 90 s timeout per tool call, 45 s per HTTP request, 2 retries on 429/5xx.
- `search_carriers`: name of 3+ characters, at most 100 results (scans up to 500 matches to sort by fleet size).
- History lists are capped (50 status changes, 50 insurance filings, 25 cancellations, 50 orders, 10 shared-contact examples).
- Inspection/crash numbers are raw counts from FMCSA files, not the official SMS BASIC percentiles.
- One SODA query per dataset per call; `red_flags` and `check_authority` make about 10 to 13 requests and usually take 5 to 12 s.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/server.py                         # streamable-http on 0.0.0.0:8080, endpoint /mcp, GET /health
PORT=8094 .venv/bin/python src/server.py               # other port
MCP_TRANSPORT=stdio .venv/bin/python src/server.py     # stdio, for local MCP clients
.venv/bin/python tests/live_test.py                    # live test of every tool
```

Environment variables: `HOST` (default `0.0.0.0`), `PORT` (default `8080`), `MCP_TRANSPORT` (`streamable-http`, `stdio` or `sse`; default `streamable-http`). No API key is needed.

`mcpize.yaml` is included for MCPize (`runtime: python`, `startCommand.type: http`).

## Use with Claude Desktop or another MCP client

Local (stdio). Clone the repo, create the venv as above, then add this to `claude_desktop_config.json`
(or your client's MCP config), with the paths changed to where you cloned it:

```json
{
  "mcpServers": {
    "fmcsa-carrier": {
      "command": "/path/to/fmcsa-carrier-mcp/.venv/bin/python",
      "args": ["/path/to/fmcsa-carrier-mcp/src/server.py"],
      "env": {"MCP_TRANSPORT": "stdio"}
    }
  }
}
```

Remote (streamable HTTP). Run `.venv/bin/python src/server.py` on a server and point any client that supports
remote MCP servers at `http://<host>:8080/mcp`. `GET /health` returns `{"status": "healthy"}`.

## License

MIT, see [LICENSE](LICENSE). The data returned by the tools stays under the terms of its sources (see above).
