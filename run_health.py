"""Watchdog for the scheduled runs: email when a run fails, never runs,
or a position on Alpaca has no stop order.

  python run_health.py start <job>                 a run began
  python run_health.py finish <job> ok|warn|failed [message] [--log FILE]
  python run_health.py check                       look for problems

Jobs: morning_buy (8:45 AM Central) and daily_scan (5:30 PM Central),
weekdays (scripts/install_daily_task.ps1). The scheduled scripts call
start / finish; `failed` and `warn` email at once with the end of the log.
The "TradingBot Watchdog" task runs `check` at 9:30 AM and 8:00 PM. It
emails when the last scheduled run did not finish OK (a missed run, or one
that started and was stopped, as on 2026-11-03), and when an Alpaca
position (other than parked SPY) has fewer shares under stop orders than
it holds (REGN had no stop on 2026-11-02). Each problem is emailed once.
The dashboard shows the run problems under System notes.

A PC that is off cannot send anything: the watchdog task starts when the
PC is back (StartWhenAvailable) and reports what was missed then.
"""

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from market_clock import CENTRAL
from project_paths import DATA_DIR

STATUS_PATH = DATA_DIR / "run_status.json"

# Scheduled time (Central) and how long a run may take before it counts
# as missed.
JOBS = {
    "morning_buy": {
        "label": "Morning buy",
        "at": time(8, 45),
        "grace": timedelta(minutes=30),
    },
    "daily_scan": {
        "label": "Evening scan",
        "at": time(17, 30),
        "grace": timedelta(minutes=90),
    },
}
# A run that starts this much before its scheduled time is a manual run,
# not the scheduled one.
EARLY_START = timedelta(minutes=30)
LOG_TAIL_LINES = 40
STOP_ORDER_TYPES = {"stop", "stop_limit", "trailing_stop"}
FINISHED_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}


def _now():
    return datetime.now(timezone.utc)


def _parse(value):
    return datetime.fromisoformat(value) if value else None


def _status_path():
    return Path(os.getenv("RUN_STATUS_PATH") or STATUS_PATH)


def load_status(path=None):
    path = Path(path or _status_path())
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    status.setdefault("jobs", {})
    status.setdefault("alerts_sent", [])
    return status


def save_status(status, path=None):
    path = Path(path or _status_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the list of sent alerts short.
    status["alerts_sent"] = status["alerts_sent"][-200:]
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    temp.replace(path)


def _start_tracking(status, now):
    # Runs scheduled before the watchdog existed are not reported.
    status.setdefault("tracking_since", now.isoformat())


def record_start(job, now=None, path=None):
    now = now or _now()
    status = load_status(path)
    _start_tracking(status, now)
    status["jobs"][job] = {"started_at": now.isoformat(), "result": "running"}
    save_status(status, path)


def record_finish(job, result, message="", now=None, path=None):
    now = now or _now()
    status = load_status(path)
    _start_tracking(status, now)
    run = status["jobs"].setdefault(job, {"started_at": now.isoformat()})
    run.update(finished_at=now.isoformat(), result=result, message=message)
    save_status(status, path)


def last_due(job, now):
    """The latest scheduled start of `job` whose time to finish has passed."""
    spec = JOBS[job]
    local = now.astimezone(CENTRAL)
    day = local.date()
    for _ in range(10):
        if day.weekday() < 5:
            scheduled = datetime.combine(day, spec["at"], tzinfo=CENTRAL)
            if scheduled + spec["grace"] <= local:
                return scheduled
        day -= timedelta(days=1)
    return None


def run_problems(status, now=None):
    """[(alert key, message)] for scheduled runs that did not finish OK."""
    now = now or _now()
    since = _parse(status.get("tracking_since"))
    problems = []
    for job, spec in JOBS.items():
        scheduled = last_due(job, now)
        if scheduled is None or since is None or scheduled < since:
            continue
        run = status["jobs"].get(job) or {}
        started = _parse(run.get("started_at"))
        when = f"{scheduled:%a %b %d} {scheduled:%I:%M %p}".replace(" 0", " ")
        key = f"{job}:{scheduled.date().isoformat()}"
        if started is None or started < scheduled - EARLY_START:
            problems.append((
                f"{key}:missed",
                f"{spec['label']} for {when} Central did not run.",
            ))
        elif run.get("result") == "running":
            problems.append((
                f"{key}:unfinished",
                f"{spec['label']} for {when} Central started at "
                f"{started.astimezone(CENTRAL):%I:%M %p} but never finished "
                "(stopped, or the PC went to sleep or shut down).",
            ))
        elif run.get("result") == "failed":
            problems.append((
                f"{key}:failed",
                f"{spec['label']} for {when} Central failed: "
                f"{run.get('message') or 'see the log in logs/'}",
            ))
    return problems


def _value(item):
    """Alpaca enums or plain strings -> lower-case string."""
    return str(getattr(item, "value", item) or "").lower()


def unprotected_positions(client, park_symbol="SPY"):
    """[(ticker, shares held, shares under open sell stop orders)] for
    each Alpaca position not fully covered by stop orders."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    positions = [
        position for position in client.get_all_positions()
        if position.symbol != park_symbol and float(position.qty) > 0
    ]
    if not positions:
        return []

    # The "open" list leaves out a bracket's stop leg while it is held
    # (FTNT, 2026-11-05), so read every order of the held stocks with its
    # legs and count each live stop once.
    symbols = [position.symbol for position in positions]
    stack = list(client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.ALL, nested=True, symbols=symbols, limit=500,
    )))
    covered = {}
    seen = set()
    while stack:
        order = stack.pop()
        stack.extend(getattr(order, "legs", None) or [])
        if order.id in seen:
            continue
        seen.add(order.id)
        if (
            _value(order.side) == "sell"
            and _value(order.order_type) in STOP_ORDER_TYPES
            and _value(order.status) not in FINISHED_STATUSES
        ):
            covered[order.symbol] = covered.get(order.symbol, 0) + float(order.qty or 0)

    return [
        (position.symbol, float(position.qty), covered.get(position.symbol, 0))
        for position in positions
        if covered.get(position.symbol, 0) < float(position.qty)
    ]


def stop_problems(now=None):
    """[(alert key, message)] for positions without a full stop order."""
    from broker import auto_trading_status, get_trading_client
    from cash_parking import PARK_SYMBOL

    enabled, _ = auto_trading_status()
    if not enabled:
        return []
    today = (now or _now()).astimezone(CENTRAL).date().isoformat()
    try:
        gaps = unprotected_positions(get_trading_client(), PARK_SYMBOL)
    except Exception as exc:  # network and API errors
        return [(
            f"stops:lookup:{today}",
            f"Could not check the stop orders on Alpaca: {exc}",
        )]
    return [
        (
            f"stops:{ticker}:{today}",
            f"{ticker}: {held:g} shares held but only {stops:g} under a stop "
            "order on Alpaca. Run `auto_trader.py --sync` or set a stop by hand.",
        )
        for ticker, held, stops in gaps
    ]


def _send(subject, body):
    from scan_notifications import send_email

    try:
        return send_email(subject, body)
    except RuntimeError as exc:
        print(f"Alert email failed: {exc}", file=sys.stderr)
        return False


def _log_tail(log_path):
    if not log_path:
        return ""
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n\nEnd of the log:\n" + "\n".join(lines[-LOG_TAIL_LINES:])


def alert_finish(job, result, message, log_path=None):
    """Email right after a run that failed or finished with problems."""
    label = JOBS.get(job, {}).get("label", job)
    word = "FAILED" if result == "failed" else "finished with problems"
    _send(
        f"TradingBot: {label} {word}",
        f"{label} {word} at {_now().astimezone(CENTRAL):%a %b %d %I:%M %p} Central."
        f"\n\n{message or ''}"
        + (f"\n\nLog: {log_path}" if log_path else "")
        + _log_tail(log_path),
    )


def check(now=None, path=None, include_stops=True):
    """Email each new problem once; returns every current problem."""
    now = now or _now()
    status = load_status(path)
    _start_tracking(status, now)
    problems = run_problems(status, now)
    if include_stops:
        problems += stop_problems(now)
    new = [(key, text) for key, text in problems if key not in status["alerts_sent"]]
    if new:
        body = "\n".join(f"- {text}" for _, text in new)
        if _send(f"TradingBot: {len(new)} problem(s) need a look", body
                 + "\n\nLogs are in the logs/ folder of the TradingBot project."):
            status["alerts_sent"].extend(key for key, _ in new)
    save_status(status, path)
    return problems


def main(argv=None):
    from dotenv import load_dotenv

    from project_paths import BASE_DIR

    load_dotenv(BASE_DIR / ".env")
    argv = list(sys.argv[1:] if argv is None else argv)
    log_path = None
    if "--log" in argv:
        index = argv.index("--log")
        log_path = argv[index + 1] if index + 1 < len(argv) else None
        del argv[index:index + 2]

    command = argv[0] if argv else "check"
    if command == "start" and len(argv) >= 2:
        record_start(argv[1])
        return 0
    if command == "finish" and len(argv) >= 3:
        job, result = argv[1], argv[2].lower()
        if result not in {"ok", "warn", "failed"}:
            print("Result must be ok, warn or failed.", file=sys.stderr)
            return 2
        message = " ".join(argv[3:])
        record_finish(job, result, message)
        if result != "ok":
            alert_finish(job, result, message, log_path)
        return 0
    if command == "check":
        problems = check()
        for _, text in problems:
            print("PROBLEM:", text)
        if not problems:
            print("All scheduled runs finished OK; every position has a stop.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
