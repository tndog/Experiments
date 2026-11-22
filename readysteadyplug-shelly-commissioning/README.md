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

### Rebooting a SIM (`--reboot`)

The API's connection reset (`POST /simcards/{iccid}/reset`) is unreliable. `--reboot` instead power-cycles the SIM by disabling it and then re-enabling it:

```bash
python3 simbase_status.py 8944500000000000001 --reboot
python3 simbase_status.py --tag site-42 --reboot --yes    # several SIMs: --yes is required
```

1. Checks the account balance (`GET /account/balance`) and refuses to start if the balance is below `--min-balance` (default 1.0) and auto top-up is off. Without this check, the re-enable could fail with `402` and leave the device offline.
2. Skips any SIM that isn't `enabled`, so a SIM that was disabled on purpose is never switched back on.
3. `POST /simcards/{iccid}/state {"state":"disabled"}`, then polls until `state=disabled`.
4. Stays off for `--off-seconds` (default 10).
5. `POST /simcards/{iccid}/state {"state":"enabled"}`, retrying on `409` while the previous change settles, then polls until `state=enabled`.
6. Waits up to `--online-timeout` (default 300s, `0` to skip) for `session_status=in_session`.

Outcomes:
- `ONLINE`: back in a data session.
- `REBOOTED`: re-enabled, with the wait for a data session skipped (`--online-timeout 0`).
- `NOT_ONLINE`: re-enabled, but no data session yet.
- `SKIPPED`: the SIM wasn't enabled, so it was left alone.
- `FAILED`: the SIM didn't reach the expected state, and the reason says if it was **left disabled**.
- `ERROR`: an API call failed.

Progress goes to stderr and the summary goes to stdout (`--json` works too).

The API key needs the scopes `simcards.details:read` and `simcards.state:write`, plus `account:read` for the balance check. Without `account:read`, the script prints a warning and skips the check.

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
| `POST /simcards/{iccid}/state` | Enables or disables the SIM with body `{"state":"enabled"\|"disabled"}`. The change is asynchronous: it returns `202` and the state passes through `enabling` or `disabling`. It returns `409` if a change is already in progress and `402` if the balance is too low to enable. Needs `simcards.state:write`. |
| `GET /account/balance` | `balance`, `currency` and `auto_top_up_enabled`. Needs `account:read`. |
| `GET /integrations/events` + destinations | Event feed (webhooks follow Standard Webhooks). Types cover state enabled/disabled, IMEI changed, SMS MO/MT, usage CDR and usage threshold exceeded. **There are no online/offline session events**, so check live status by polling. |
