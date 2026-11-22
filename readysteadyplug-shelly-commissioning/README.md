# readysteadyplug – Shelly commissioning

Tools for commissioning Shelly devices on Simbase SIMs, starting with a SIM connection-status check for RSP connectivity.

## `simbase_status.py`

This script checks whether Simbase SIMs are connected. It uses only the Python 3.9+ standard library, with no dependencies.

```bash
export SIMBASE_API_KEY=...            # scope: simcards.details:read
python3 simbase_status.py 8944500000000000001 8944500000000000002
python3 simbase_status.py --tag site-42           # every SIM with a tag
python3 simbase_status.py --all --json            # whole account, machine-readable
python3 simbase_status.py <iccid> --history 10    # show the last 10 network attaches
```

Exit codes: `0` means every SIM is ONLINE, `1` means at least one is not, and `2` means an error (bad key, network failure, or every lookup failed). You can use the exit code as a pass/fail gate in a commissioning script.

### Verdicts

| Verdict | Rule |
|---|---|
| `ONLINE` | `state=enabled` and `session_status=in_session` |
| `ATTACHED_IDLE` | Enabled with no data session, but `connection.last_update` falls within `--stale-minutes` (default 60). The SIM is registered but not passing data. Check the APN and the device. |
| `OFFLINE` | Enabled with no data session, and no network update within the window (or never seen) |
| `REJECTED` | The latest location update has `connection_result=rejected`, usually from an IMEI lock after the SIM was moved to another device |
| `DISABLED` | `state` is `disabled`, `enabling` or `disabling` |
| `UNKNOWN` | `session_status=unavailable` |

## Simbase API v2 notes

These notes come from the OpenAPI spec at <https://developer.simbase.com/simbase-api-v2.json>, version 2026-11-18.

- **Base URL:** `https://api.simbase.com/v2`
- **Auth:** `Authorization: Bearer <API key>`. Create keys in the dashboard under Integrations > API. Scopes are fixed when a key is created. A bad key returns `401` and a missing scope returns `403`.
- **Rate limits:** 10 calls per second and 5000 calls per day per account. The script uses 2 calls per SIM, or 1 with `--history 0`.
- **Pagination:** cursor-based, with `limit` and `cursor`. The response carries `cursor` (null when finished) and `has_more`.

| Endpoint | Use for connectivity |
|---|---|
| `GET /simcards/{iccid}` | Returns `state`, `session_status` (`in_session` / `no_session` / `unavailable`), `connection` {carrier, country, mcc, mnc, last_update}, `location` {radio, cell_id, lac, lat, lon, last_update}, `imei`, `hardware`, `imei_lock`, `public_ip`, `private_network_ip`, `current_month_usage` |
| `GET /simcards` | Lists SIMs with filters for `state`, `coverage` and `tags`. **Does not include `session_status` or `connection`**, so fetch details for each SIM. |
| `GET /simcards/{iccid}/location-updates` | Attach history for the last 90 days: `timestamp`, `connection_result` (allowed/rejected), `type` (RAT), mcc/mnc, `cell_id`, `lac`, `imei` |
| `GET /simcards/{iccid}/cdrs` | Usage records (data sessions), filterable by `month` |
| `POST /simcards/{iccid}/reset` | Forces a network detach and reattach. It does not always work; disabling and then re-enabling the SIM is more reliable. Needs `simcards.reset:write`. |
| `POST /simcards/{iccid}/state` | Enables or disables the SIM |
| `GET /integrations/events` + destinations | Event feed (webhooks follow Standard Webhooks). Types cover state enabled/disabled, IMEI changed, SMS MO/MT, usage CDR and usage threshold exceeded. **There are no online/offline session events**, so check live status by polling. |
