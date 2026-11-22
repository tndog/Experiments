#!/usr/bin/env python3
"""Check Simbase SIM connection status for RSP connectivity commissioning.

Uses the Simbase API v2 (https://developer.simbase.com/). Standard library only.

Auth: set SIMBASE_API_KEY to an API key with scope `simcards.details:read`
(create one in the Simbase dashboard under Integrations > API).

Examples:
    simbase_status.py 8944500000000000001 8944500000000000002
    simbase_status.py --tag site-42
    simbase_status.py --all --json
    simbase_status.py 8944500000000000001 --history 5
    simbase_status.py 8944500000000000001 --reboot      # disable, re-enable, wait for online

Exit codes: 0 = every SIM checked (or rebooted) is ONLINE, 1 = at least one is not, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = os.environ.get("SIMBASE_BASE_URL", "https://api.simbase.com/v2").rstrip("/")

# Verdicts, roughly from best to worst.
ONLINE = "ONLINE"            # enabled and in an active data session
ATTACHED = "ATTACHED_IDLE"   # registered on a network recently, but no data session
OFFLINE = "OFFLINE"          # no session and no recent network registration
REJECTED = "REJECTED"        # latest network attach was rejected (e.g. IMEI lock)
DISABLED = "DISABLED"        # SIM not enabled (disabled / enabling / disabling)
UNKNOWN = "UNKNOWN"          # Simbase could not report the session status


class ApiError(Exception):
    pass


class ApiStatusError(ApiError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def api_get(path: str, api_key: str, params: dict | None = None, retries: int = 4) -> dict:
    return api_request("GET", path, api_key, params=params, retries=retries)


def api_request(method: str, path: str, api_key: str, params: dict | None = None,
                body: dict | None = None, retries: int = 4) -> dict:
    query = ""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        query = "?" + urllib.parse.urlencode(clean, doseq=True)
    url = f"{BASE_URL}{path}{query}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # Default limits: 10 calls/s, 5000 calls/day per account.
            if e.code == 429 and attempt < retries:
                wait = float(e.headers.get("Retry-After") or 2 ** attempt)
                time.sleep(wait)
                continue
            err_body = e.read().decode(errors="replace")[:300]
            hint = {
                401: "invalid or missing API key",
                403: "API key lacks the scope this call needs",
                404: "SIM not found in this account",
                402: "insufficient account balance",
                409: "SIM state is currently being updated",
            }.get(e.code, "")
            raise ApiStatusError(
                e.code, f"HTTP {e.code} for {method} {path}{' (' + hint + ')' if hint else ''}: {err_body}"
            ) from None
        except urllib.error.URLError as e:
            # Only retry network errors for reads; a POST may have gone through.
            if attempt < retries and method == "GET":
                time.sleep(2 ** attempt)
                continue
            raise ApiError(f"Network error for {path}: {e.reason}") from None
    raise ApiError(f"Gave up on {path}")


def list_iccids(api_key: str, tags: list[str] | None) -> list[str]:
    iccids, cursor = [], None
    while True:
        page = api_get("/simcards", api_key, {"tags": tags or None, "limit": 250, "cursor": cursor})
        iccids += [s["iccid"] for s in page.get("simcards", [])]
        cursor = page.get("cursor")
        if not cursor:
            return iccids


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_minutes(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


def classify(sim: dict, latest_lu: dict | None, stale_minutes: float) -> tuple[str, str]:
    state = sim.get("state")
    session = sim.get("session_status")
    conn = sim.get("connection") or {}
    conn_age = age_minutes(parse_ts(conn.get("last_update")))

    if state != "enabled":
        return DISABLED, f"SIM state is '{state}'"
    if latest_lu and latest_lu.get("connection_result") == "rejected":
        return REJECTED, f"last attach rejected (IMEI {latest_lu.get('imei') or '?'}, imei_lock={sim.get('imei_lock')})"
    if session == "in_session":
        return ONLINE, "active data session"
    if session == "unavailable":
        return UNKNOWN, "session status unavailable from network"
    if conn_age is not None and conn_age <= stale_minutes:
        return ATTACHED, f"no data session; last network update {conn_age:.0f} min ago"
    if conn_age is None:
        return OFFLINE, "no data session; never seen on a network"
    return OFFLINE, f"no data session; last network update {conn_age:.0f} min ago"


def check_sim(iccid: str, api_key: str, stale_minutes: float, history: int) -> dict:
    sim = api_get(f"/simcards/{urllib.parse.quote(iccid)}", api_key)
    lus = []
    if history > 0:
        lu_page = api_get(f"/simcards/{urllib.parse.quote(iccid)}/location-updates", api_key, {"limit": history})
        lus = lu_page.get("location_updates", [])
    verdict, reason = classify(sim, lus[0] if lus else None, stale_minutes)
    conn = sim.get("connection") or {}
    loc = sim.get("location") or {}
    usage = sim.get("current_month_usage") or {}
    return {
        "iccid": sim.get("iccid", iccid),
        "name": sim.get("name"),
        "verdict": verdict,
        "reason": reason,
        "state": sim.get("state"),
        "session_status": sim.get("session_status"),
        "carrier": conn.get("carrier"),
        "country": conn.get("country"),
        "mcc_mnc": f"{conn['mcc']}-{conn['mnc']}" if conn.get("mcc") else None,
        "last_network_update": conn.get("last_update"),
        "radio": loc.get("radio") or (lus[0].get("type") if lus else None),
        "imei": sim.get("imei"),
        "hardware": sim.get("hardware"),
        "imei_lock": sim.get("imei_lock"),
        "public_ip": sim.get("public_ip"),
        "private_network_ip": sim.get("private_network_ip"),
        "month_data_bytes": usage.get("data"),
        "month_data_sessions": usage.get("data_sessions"),
        "tags": sim.get("tags"),
        "location_updates": lus,
    }


def fmt_bytes(n) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# ---------------------------------------------------------------------------
# Reboot: disable the SIM, wait, re-enable it, wait until it is back online.
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def check_balance(api_key: str, min_balance: float) -> None:
    """Abort before disabling if re-enabling could fail with 402 (insufficient balance)."""
    try:
        bal = api_get("/account/balance", api_key)
    except ApiStatusError as e:
        if e.status == 403:
            log("WARNING: API key lacks scope account:read; cannot check balance before reboot")
            return
        raise
    balance = float(bal.get("balance", 0))
    log(f"Account balance {bal.get('balance')} {bal.get('currency')}"
        f"{' (auto top-up on)' if bal.get('auto_top_up_enabled') else ''}")
    if balance < min_balance and not bal.get("auto_top_up_enabled"):
        raise ApiError(
            f"Balance {balance} is below --min-balance {min_balance} and auto top-up is off. "
            "Re-enabling could fail (402) and leave SIMs offline; top up first."
        )


def set_state(iccid: str, api_key: str, state: str, retries_409: int, interval: float) -> None:
    for attempt in range(retries_409 + 1):
        try:
            api_request("POST", f"/simcards/{urllib.parse.quote(iccid)}/state", api_key, body={"state": state})
            return
        except ApiStatusError as e:
            if e.status == 409 and attempt < retries_409:
                time.sleep(interval)
                continue
            raise


def wait_for(iccid: str, api_key: str, predicate, timeout: float, interval: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        sim = api_get(f"/simcards/{urllib.parse.quote(iccid)}", api_key)
        if predicate(sim):
            return sim
        if time.monotonic() >= deadline:
            return sim
        time.sleep(interval)


def reboot_sim(iccid: str, api_key: str, args) -> dict:
    result = {"iccid": iccid, "rebooted": False}
    sim = api_get(f"/simcards/{urllib.parse.quote(iccid)}", api_key)
    result["name"] = sim.get("name")
    label = f"{iccid} ({sim.get('name') or '-'})"

    if sim.get("state") != "enabled":
        # Never enable a SIM that someone disabled on purpose, or one mid-transition.
        result.update(outcome="SKIPPED", reason=f"SIM state is '{sim.get('state')}', not 'enabled'; not touched")
        log(f"{label}: skipped, state is {sim.get('state')}")
        return result

    t0 = time.monotonic()
    log(f"{label}: disabling")
    set_state(iccid, api_key, "disabled", retries_409=0, interval=args.poll_interval)
    sim = wait_for(iccid, api_key, lambda s: s.get("state") == "disabled", args.state_timeout, args.poll_interval)
    if sim.get("state") != "disabled":
        result.update(outcome="FAILED", reason=f"SIM did not reach 'disabled' within {args.state_timeout:.0f}s "
                                               f"(state '{sim.get('state')}'); re-enable it manually if needed")
        log(f"{label}: {result['reason']}")
        return result
    log(f"{label}: disabled after {time.monotonic() - t0:.0f}s; holding off for {args.off_seconds:.0f}s")
    time.sleep(args.off_seconds)

    log(f"{label}: enabling")
    try:
        # 409 can still occur briefly right after the disable settles.
        set_state(iccid, api_key, "enabled", retries_409=6, interval=args.poll_interval)
    except ApiError as e:
        result.update(outcome="FAILED", reason=f"SIM LEFT DISABLED: re-enable failed: {e}")
        log(f"{label}: {result['reason']}")
        return result
    sim = wait_for(iccid, api_key, lambda s: s.get("state") == "enabled", args.state_timeout, args.poll_interval)
    if sim.get("state") != "enabled":
        result.update(outcome="FAILED", reason=f"SIM did not reach 'enabled' within {args.state_timeout:.0f}s "
                                               f"(state '{sim.get('state')}')")
        log(f"{label}: {result['reason']}")
        return result
    result["rebooted"] = True
    log(f"{label}: enabled after {time.monotonic() - t0:.0f}s total")

    if args.online_timeout <= 0:
        result.update(outcome="REBOOTED", reason="SIM disabled and re-enabled; did not wait for a data session")
        return result
    log(f"{label}: waiting up to {args.online_timeout:.0f}s for a data session")
    sim = wait_for(iccid, api_key, lambda s: s.get("session_status") == "in_session",
                   args.online_timeout, args.poll_interval)
    if sim.get("session_status") == "in_session":
        result.update(outcome="ONLINE", reason=f"back online {time.monotonic() - t0:.0f}s after disable")
    else:
        result.update(outcome="NOT_ONLINE", reason=f"re-enabled, but no data session after {args.online_timeout:.0f}s "
                                                   f"(session_status '{sim.get('session_status')}')")
    log(f"{label}: {result['outcome']} - {result['reason']}")
    return result


def run_reboot(iccids: list[str], api_key: str, args) -> int:
    if len(iccids) > 1 and not args.yes:
        print(f"--reboot would take {len(iccids)} SIMs offline; pass --yes to confirm.", file=sys.stderr)
        return 2
    check_balance(api_key, args.min_balance)
    results = []
    for iccid in iccids:
        try:
            results.append(reboot_sim(iccid, api_key, args))
        except ApiError as e:
            results.append({"iccid": iccid, "rebooted": False, "outcome": "ERROR", "reason": str(e)})
            log(f"{iccid}: ERROR {e}")
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{r['iccid']}  {r.get('name') or '-'}  {r['outcome']}  - {r['reason']}")
    if all(r["outcome"] in ("ONLINE", "REBOOTED") for r in results):
        return 0
    return 2 if all(r["outcome"] == "ERROR" for r in results) else 1


def print_table(results: list[dict]) -> None:
    cols = [
        ("ICCID", "iccid"), ("NAME", "name"), ("VERDICT", "verdict"), ("CARRIER", "carrier"),
        ("RAT", "radio"), ("LAST SEEN (UTC)", "last_network_update"), ("IP", "public_ip"),
        ("DATA MTD", "month_data_bytes"),
    ]
    rows = []
    for r in results:
        row = []
        for _, key in cols:
            v = r.get(key)
            if key == "month_data_bytes":
                v = fmt_bytes(v)
            elif key == "last_network_update" and v:
                dt = parse_ts(v)
                v = dt.strftime("%Y-%m-%d %H:%M") if dt else v
            elif key == "public_ip" and not v:
                v = r.get("private_network_ip")
            row.append("-" if v in (None, "") else str(v))
        rows.append(row)
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, (h, _) in enumerate(cols)]
    print("  ".join(h.ljust(w) for (h, _), w in zip(cols, widths)))
    for row, r in zip(rows, results):
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))
        print(f"    -> {r['reason']}")
        for lu in r.get("location_updates", []):
            print(f"       {lu.get('timestamp')}  {lu.get('connection_result'):8} {lu.get('type') or '-':5} "
                  f"{lu.get('country_code')} {lu.get('mcc')}-{lu.get('mnc')} cell={lu.get('cell_id') or '-'} "
                  f"imei={lu.get('imei') or '-'}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("iccids", nargs="*", help="ICCIDs to check")
    p.add_argument("--tag", action="append", help="check all SIMs with this tag (repeatable)")
    p.add_argument("--all", action="store_true", help="check every SIM in the account")
    p.add_argument("--stale-minutes", type=float, default=60,
                   help="network update older than this counts as OFFLINE (default 60)")
    p.add_argument("--history", type=int, default=1,
                   help="location updates to fetch per SIM (0 to skip; default 1, used to detect rejected attaches)")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    r = p.add_argument_group("reboot (disable, then re-enable the SIM)")
    r.add_argument("--reboot", action="store_true",
                   help="power-cycle the SIM via the API instead of only checking it "
                        "(needs scope simcards.state:write; account:read for the balance check)")
    r.add_argument("--yes", action="store_true", help="confirm rebooting more than one SIM")
    r.add_argument("--off-seconds", type=float, default=10, help="time to stay disabled (default 10)")
    r.add_argument("--state-timeout", type=float, default=300,
                   help="max seconds to wait for each disable/enable to complete (default 300)")
    r.add_argument("--online-timeout", type=float, default=300,
                   help="max seconds to wait for a data session after re-enable; 0 to skip (default 300)")
    r.add_argument("--poll-interval", type=float, default=5, help="seconds between status polls (default 5)")
    r.add_argument("--min-balance", type=float, default=1.0,
                   help="refuse to reboot if balance is below this and auto top-up is off (default 1.0)")
    args = p.parse_args()

    api_key = os.environ.get("SIMBASE_API_KEY")
    if not api_key:
        print("SIMBASE_API_KEY is not set", file=sys.stderr)
        return 2
    if not (args.iccids or args.tag or args.all):
        p.error("give one or more ICCIDs, --tag, or --all")

    try:
        iccids = list(args.iccids)
        if args.tag or args.all:
            iccids += [i for i in list_iccids(api_key, args.tag) if i not in iccids]
        if not iccids:
            print("No SIM cards matched.", file=sys.stderr)
            return 2
        if args.reboot:
            return run_reboot(iccids, api_key, args)
        results = []
        for iccid in iccids:
            try:
                results.append(check_sim(iccid, api_key, args.stale_minutes, args.history))
            except ApiError as e:
                results.append({"iccid": iccid, "verdict": "ERROR", "reason": str(e), "location_updates": []})
    except ApiError as e:
        print(e, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results)
        counts: dict[str, int] = {}
        for r in results:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    if any(r["verdict"] == "ERROR" for r in results) and not any(r["verdict"] != "ERROR" for r in results):
        return 2
    return 0 if all(r["verdict"] == ONLINE for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
