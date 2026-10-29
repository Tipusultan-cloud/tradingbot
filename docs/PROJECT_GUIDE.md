# TradingBot: step-by-step project guide

This guide follows the project in the order it is meant to be used: configure,
start, scan, review, and then test or back up. TradingBot is decision support;
it does not submit brokerage orders.

## 1. Know where things belong

- Root-level Python files are the runnable application and scanner entry
  points. They stay at the root so the current imports and commands continue
  to work.
- `scripts/` contains the PowerShell startup, scheduled-scan, and backup
  implementations.
- `data/` contains the latest generated universe, scan output, and master
  selection. The dashboard and webhook read the current files from here.
- `data/archive/root_snapshots/` holds older copies that used to sit in the
  project root. They are historical references; do not use them as current
  scan results.
- `web/` contains the local read-only dashboard.
- `backtests/` contains backtesting programs; their CSV results go in
  `results/backtests/`.
- `logs/` contains daily scan logs.
- `tests/` contains automated checks.

## 2. Configure the local environment

1. Open the project folder in VS Code.
2. Use the existing `.venv` Python environment. If setting up on a new
   computer, create a virtual environment and install the pinned packages:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Create `.env` from `.env.example` if it does not exist.
4. Set `TV_WEBHOOK_SECRET` to a long, unique secret. Keep `.env` private; do
   not paste it into chat, source control, or a shared backup.
5. Review the paper-account and risk values in `.env` before using signals.

## 3. Start the machine

1. Double-click `Start TradingBot.bat`, or run
   `scripts/start_tradingbot.ps1` in PowerShell.
2. The startup script starts:
   - Webhook API at `http://127.0.0.1:8000`
   - Local-only dashboard at `http://127.0.0.1:8001`
   - The daily scan pipeline
   - ngrok, if its executable is installed at the configured path
3. Open `http://127.0.0.1:8001` to view the dashboard.
4. The ngrok tunnel forwards port 8000 for TradingView webhooks. Never forward
   port 8001; it has no login and is intended only for this computer.
   Dashboard data refreshes automatically every 30 seconds. The **Refresh
   data** button reloads the existing results and journal; it does not start a
   scan. Open paper positions show risk to their recorded stops, which are not
   automatically monitored or executed.
   Every recorded entry and exit also updates `data/paper_trades.csv`; use
   **Download all trades (CSV)** above the history table to open the complete
   journal in Excel. The SQLite journal remains the source of truth.
5. The weekday schedule is already registered on this computer for 8:30 AM
   local time. On a new computer, double-click `Install Daily Scan Task.bat`
   to create it. The task runs while this Windows user is logged in.
6. Confirm `TradingBot Daily Scan` is Ready in Windows Task Scheduler and has
   the expected next run time. Keep the computer powered on and the Windows
   user signed in at scan time. If Windows denies permission to update an
   existing task, run the installer from an elevated PowerShell window.

## 4. Understand the daily scan order

Run the stages in sequence. Each stage uses output from the preceding stage:

1. `universe_builder.py` ranks market data and writes the current 200-stock
   universe to `data/top_200_universe.json` and `.csv`.
2. `scanner.py` runs the original technical scan and writes
   `data/qualified_setups.json`.
3. `scanner_topdown.py` checks market, sectors, and stocks and writes
   `data/qualified_setups_topdown.json`.
4. `master_selector.py` compares scanner results and writes
   `data/master_candidates.json` and `data/best_current_candidate.json`.
5. `paper_exits.py` closes open paper trades that hit their stop, 2R target,
   or 20-day limit, so their slots can be refilled.
6. `scanner_pullback.py` finds S&P 500 leaders (top 20% by 6-month return,
   in an uptrend) that fell more than 5% this week and writes
   `data/pullback_setups.json`.
7. `slot_selector.py` fills free paper-trade slots (`MAX_OPEN_TRADES`,
   default 2) with the A+ pick first, then pullbacks, and writes
   `data/slot_selection.json`.

Stages 5-7 only warn on failure; the A+ pipeline is unaffected.

The scheduled runner is `scripts/scheduled_daily_scan.ps1`. The root-level
`scheduled_daily_scan.ps1` is kept as a compatibility entry point for
Task Scheduler setups that already reference it.
It uses a mutex so scheduled and manually started scans do not overlap. After
all four scan stages succeed, it sends an email if SMTP is configured. No SMTP
settings means email is skipped; incomplete or failed settings are reported
in the scan log.

To run the stages manually:

```powershell
.\.venv\Scripts\python.exe -u .\universe_builder.py
.\.venv\Scripts\python.exe -u .\scanner.py
.\.venv\Scripts\python.exe -u .\scanner_topdown.py
.\.venv\Scripts\python.exe -u .\master_selector.py
.\.venv\Scripts\python.exe -u .\paper_exits.py
.\.venv\Scripts\python.exe -u .\scanner_pullback.py
.\.venv\Scripts\python.exe -u .\slot_selector.py
```

## 5. Review the decision

1. Check the dashboard's market regime and pipeline timestamps.
2. Check `data/best_current_candidate.json` for the latest WAIT or candidate
   decision.
3. Check `data/master_candidates.json` for approved and rejected candidates.
4. Check the dashboard's **Trade slots** panel (or
   `data/slot_selection.json`) for the stocks picked for free slots, with
   entry reference, stop, target, and shares.
5. A zero-candidate result is valid: the decision is **NO TRADE**.
6. Confirm data is fresh before relying on it. The webhook rejects missing or
   stale scanner/master/slot outputs, and rejects any BUY while all slots
   hold open paper trades.

The master selector currently chooses a best current candidate, not a
persistent one-trade-per-week choice. Its `weekly_lock` is disabled; daily
scans can replace the candidate.

## 6. Turn on automatic trading (Alpaca paper, optional)

With this on, the bot buys its slot picks itself and the dashboard gets a
**Buy** button on every stock in the Bot 1 / Bot 2 lists. No real money is
used, and TradingView alerts are ignored while it is on.

1. Create a free account at alpaca.markets and open the **Paper Trading**
   dashboard. Optionally reset the paper balance to the amount you want to
   practice with.
2. In the paper dashboard, generate API keys. Paper keys start with `PK`.
3. In `.env`, set `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_PAPER=true`
   and `AUTO_TRADING=true`. Set `PAPER_ACCOUNT_BALANCE` to the balance that
   position sizes should use (1% of it is risked per trade).
4. Restart TradingBot. The dashboard shows **Auto trading ON** above the
   Trade slots panel.
5. The 8:30 AM scan sends orders once the market is open. Buy buttons only
   work while the market is open (9:30 AM - 4:00 PM New York).
6. Each buy has a real stop-loss and 2R target at Alpaca. Fills and exits
   appear in the journal after the next scan or dashboard refresh.

The same keys also turn on the bad-news check.

To stop automatic trading, set `AUTO_TRADING=false` and restart. Open
positions keep their stop and target orders at Alpaca.

## 7. Connect TradingView carefully

While `AUTO_TRADING=true`, the webhook ignores TradingView alerts; the steps
below apply when auto trading is off.

1. Confirm the webhook API responds at `http://127.0.0.1:8000/`.
2. Get the current HTTPS address from the ngrok terminal.
3. Configure the TradingView alert URL as the ngrok address followed by
   `/webhook`.
4. Send the secret, ticker, action, price, and ATR fields in the expected JSON
   alert body. Use the same secret configured in `.env`.
5. A BUY signal is only confirmed if the ticker is in fresh original scanner
   output and is an approved A+ stock (Bot 1 list), or is in the fresh
   pullback list (Bot 2 list), and a slot is free. You can pick any stock
   in either list, not only the bot's slot picks. A pullback paper trade is only
   recorded if a BUY alert arrives for that ticker, so set a TradingView
   alert (or send the same JSON) for each slot pick you want to follow.
   A confirmed BUY is recorded as
   a paper position in `data/paper_trades.sqlite3`; a TradingView SELL closes
   it and saves realized P&L. No live order is placed. This journal uses the
   alert price. Each daily scan closes open paper trades that hit their
   stop, 2R target, or 20-day limit (`paper_exits.py`); this is a
   once-a-day paper check, not a broker stop order.
6. To receive daily email alerts, set `SMTP_HOST`, `SMTP_PORT`,
   `SMTP_USERNAME`, `SMTP_PASSWORD`, and `TRADINGBOT_ALERT_EMAIL` in `.env`.
   For Gmail, use `smtp.gmail.com` on port `465`, your Gmail address as the
   username, and a Google App Password as the SMTP password. Enable
   2-Step Verification in your Google Account before creating an App Password.
   Keep the app password private; do not use your normal Gmail password.
   Each successful scan sends the best current candidate or a NO TRADE
   summary. The scheduled task does not send an email for each TradingView
   webhook.

Treat the ngrok URL and webhook secret as private. Do not share the URL or
secret in screenshots or public posts.

## 8. Run checks and backtests

Run the focused tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Scanning and the paper journal work without email configuration. Gmail may
require 2-Step Verification before it allows you to create an App Password.

Backtest scripts are in `backtests/`; result CSVs are in
`results/backtests/`. Review the date range, holdout period, drawdown, trade
count, and SPY comparison. Existing backtests represent different strategy
versions and do not by themselves validate the exact current master rules.

## 9. Make a backup

1. Double-click `Backup TradingBot.bat`, or run
   `scripts/backup_tradingbot.ps1`.
2. The archive is written under `backups/`; that folder is ignored by Git.
3. `.env` is intentionally excluded from the archive because it contains
   secrets. Preserve the real `.env` separately in an approved secure
   password manager or encrypted storage.
4. The `.venv` folder is excluded. Rebuild it from `requirements.txt` when
   restoring on a new machine.

## 10. Troubleshoot in order

- **Dashboard won't open:** check that port 8001 is listening and start the
  dashboard with `uvicorn app:dashboard_app --host 127.0.0.1 --port 8001`.
- **Webhook health check fails:** start `uvicorn app:app --host 127.0.0.1
  --port 8000` and confirm `TV_WEBHOOK_SECRET` is set.
- **No current candidate:** review `market.bullish`, `qualified`, and
  `approved` in the current JSON files. The intended response may be NO TRADE.
- **Webhook says stale or missing results:** rerun all four scan stages in
  order and check `logs/` for failures.
- **ngrok doesn't start:** verify the ngrok executable path in
  `scripts/start_tradingbot.ps1`; the local API and dashboard can run without
  a public tunnel.
- **Scheduled task stopped working after a move:** keep its program pointing
  to the root `scheduled_daily_scan.ps1` compatibility wrapper.
- **No email arrives:** check the SMTP settings in `.env`, ensure the Gmail
  App Password is correct, and inspect the latest `logs/daily_scan_*.log`.
- **Scheduled scan did not run:** check `TradingBot Daily Scan` history in
  Task Scheduler, confirm you were signed in at the scheduled time, and review
  the latest scan log.

## 11. Start the DevOps and IaC path

The first DevOps task is source-control and secrets hygiene. Before
initializing a private Git repository, confirm `.env` and local Terraform
state are ignored; rotate any credential that has been exposed. The modular
Azure Terraform in `infra/terraform/` provisions a Windows VM foundation with
RDP restricted to a trusted CIDR, private webhook/dashboard ports, and an
attached data disk. It does not install the app, migrate local data, or
configure unattended scheduled scans. Follow
[`infra/terraform/README.md`](../infra/terraform/README.md) to authenticate
with Azure locally, configure the trusted source IP, format the files,
validate them, and inspect the plan. The VM and its disks incur ongoing
charges while provisioned. Do not apply a plan until you understand and
approve every resource and its cost.

Suggested next steps:

1. Review the Azure plan and cost, then explicitly apply it when ready.
2. Configure Windows, initialize the data disk, and deploy source code and
   secrets using a private method.
3. Verify the webhook remains available only through the outbound tunnel and
   the dashboard remains private; preserve paper-only behavior.
4. Update Task Scheduler for unattended execution under a least-privilege
   service identity.
5. Add CI for Python tests and Terraform checks, then configure secured remote
   state, backups, monitoring, and deployment approvals before production use.

Deallocating the Azure VM pauses compute charges but also stops scheduled
scans and the webhook. Starting the VM again does not deploy or start the
TradingBot processes automatically; complete the application deployment and
unattended startup work before relying on it.
