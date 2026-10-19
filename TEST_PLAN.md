# Test Plan

"Auto" = covered by `pytest -q` (102 tests, fake Daraja, run on SQLite **and** real Postgres 16
during development). "Manual" = you must run it against the real Daraja Sandbox.
Nothing in the automated tests proves a real payment; only the manual Sandbox tests do.

| # | Requirement | Type | How | Expected |
|---|---|---|---|---|
| 1 | Homepage and static assets | Auto | `test_homepage_and_static_assets` | `/`, `/style.css`, `/app.js` return 200; `/server.py`, `/.env`, `/db.py` return 404 |
| 2 | Health check | Auto + Manual | `curl https://HOST/api/test?deep=1` | `success:true`, `configured:true`, `database:ok`, no secret values |
| 3 | Missing environment variables | Auto | one test per required variable | `/api/test` says `configured:false`; `/api/pay` returns 503 and never calls Daraja |
| 4 | Invalid phone numbers | Auto | 11 bad inputs (letters, wrong prefix, wrong length, non-strings) | 400, Daraja never called |
| 5 | Bad amounts (0, negative, decimal, text, bool, huge) | Auto | 10 bad inputs + `MAX_AMOUNT` | 400, Daraja never called |
| 6 | Daraja authentication | Auto (mock) + **Manual** | `python check_setup.py --daraja` | `Daraja Sandbox login: OK` |
| 7 | Accepted STK Push | Auto (mock) + **Manual** | Section 7 of README | Prompt arrives; row stored as PENDING |
| 8 | Cancelled callback (1032) | Auto + **Manual** | Press Cancel on phone | Page shows "cancelled"; DB `CANCELLED` |
| 9 | Successful callback (0) | Auto + **Manual** | Enter PIN | Page shows success + receipt; DB `SUCCESS` |
| 10 | Other failure codes | Auto + **Manual** | Wrong PIN / let it time out | Page shows "failed" with reason; DB `FAILED` |
| 11 | Malformed callbacks | Auto | 11 junk payloads + non-JSON body | 400, payment unchanged, no crash |
| 12 | Duplicate / late / conflicting callbacks | Auto | Same result twice; success then cancel; 25 parallel on Postgres | One winner; finished result never changes; `updated_at` unchanged |
| 13 | Unknown checkout IDs | Auto | Status and callback for unknown IDs | 404 / ignored, nothing stored |
| 14 | Pending status | Auto | Status right after `/api/pay` | `PENDING` |
| 15 | Status changes after callback | Auto + **Manual** | Poll before/after callback | `PENDING` then final state |
| 16 | Timeouts and provider errors | Auto | Timeout, connection error, non-JSON, 401 token refresh | 502/504 with safe messages |
| 17 | Persistence across restarts | Auto + Manual | `test_status_survives_restart`; Render "Deploy latest commit" | Same status after restart |
| 18 | Production config | Auto | `debug` off; unexpected error hides details; production needs `DATABASE_URL` | Generic 500; no traceback or secrets |
| + | Callback authenticity | Auto | Wrong/missing token; IP allow-list | 403, payment unchanged |
| + | Amount mismatch | Auto | Success callback with different amount | Stored as `FAILED`, no receipt |
| + | Missing callback is never success | Auto | Payment 1 hour old, no answer | Still `PENDING` |
| + | STK Query fallback | Auto | Older than 30 s; throttled to once per 15 s | Result recovered; failures ignored |
| + | Rate limiting | Auto | 7 rapid `/api/pay` calls | 6th is 429 |
| + | No secrets in logs/responses/frontend | Auto | Scans logs, responses, `static/` | None found |
| + | Frontend behaviour | Auto (Node, optional) | `cd tests/frontend && npm test` | 7 pass: validation, pending not success, each state, timeout, safe text |
| + | Public callback route | **Manual** | README step 20 | 405 / 403 / 400 / 200 as listed |

## Commands

```bash
pytest -q                                  # all backend tests (SQLite)
TEST_DATABASE_URL='postgresql://...' pytest -q   # same tests on a real Postgres (use a throw-away DB: tables are dropped!)
cd tests/frontend && npm install && npm test     # optional browser-logic tests
```

## Limits of this plan

* Daraja is mocked in automated tests, so field names and behaviour were checked against
  your working code and third-party guides, not live. The Sandbox tests above close that gap.
* Daraja's STK Query on the Sandbox can be slow or unhelpful; the tests only prove our
  handling of its documented response shapes.
