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

Exit codes: 0 = every SIM checked is ONLINE, 1 = at least one is not, 2 = error.
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


def api_get(path: str, api_key: str, params: dict | None = None, retries: int = 4) -> dict:
    query = ""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        query = "?" + urllib.parse.urlencode(clean, doseq=True)
    url = f"{BASE_URL}{path}{query}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
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
            body = e.read().decode(errors="replace")[:300]
            hint = {
                401: "invalid or missing API key",
                403: "API key lacks scope simcards.details:read",
                404: "SIM not found in this account",
            }.get(e.code, "")
            raise ApiError(f"HTTP {e.code} for {path}{' (' + hint + ')' if hint else ''}: {body}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
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
