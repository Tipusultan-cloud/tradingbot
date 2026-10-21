# TradingBot
trade-machine

Purpose
TradingBot is a swing-trading decision-support project.
The system combines:
TradingView alerts
FastAPI webhook processing
Secret validation
Duplicate protection
Dynamic stock-universe building
Technical stock scanning
Market and sector confirmation
Risk-based position sizing
Backtesting
Weekly master selection
The goal is to identify one high-quality A+ swing candidate at a time. A
persistent one-trade-per-week lock is not implemented yet.
The system does not guarantee a winning trade and does not automatically place a real-money trade.
---
Current Project Flow
```text
Windows Task Scheduler
        ↓
scheduled_daily_scan.ps1
        ↓
universe_builder.py
        ↓
Build dynamic 200-stock universe
        ↓
data/top_200_universe.json
        ↓
        ┌──────────────────────┐
        │                      │
        ↓                      ↓
scanner.py              scanner_topdown.py
        ↓                      ↓
Original 4/4 scan       SPY → Sector → Stock scan
        ↓                      ↓
data/qualified_         data/qualified_setups_
setups.json             topdown.json
        │                      │
        └──────────┬───────────┘
                   ↓
            master_selector.py
                   ↓
        Apply A+ weekly rules
                   ↓
        ┌──────────┴──────────┐
        ↓                     ↓
A+ candidate found       No A+ candidate
        ↓                     ↓
Select ONE stock          NO TRADE
```
---
TradingView → FastAPI Flow
```text
TradingView
    ↓
ngrok
    ↓
FastAPI webhook
    ↓
secret validation
    ↓
input validation
    ↓
duplicate protection
    ↓
fresh scanner + master approval
    ↓
risk calculation
```
The webhook accepts BUY signals only when the ticker appears in fresh scanner
output and is the current best selection from the master selector. A missing
or stale result fails closed. SELL signals continue to be handled as exit
alerts.
---
Current Features
Webhook / API
FastAPI webhook server
TradingView BUY / SELL signal support
Webhook secret stored in `.env`
Invalid secret rejection
Ticker / action / price validation
Duplicate signal protection
ngrok public HTTPS tunnel
Market Scanning
Dynamic S&P 500 source universe
200-stock daily monitoring universe
Sector ranking
Market trend confirmation using SPY
Sector trend confirmation using sector ETFs
Original 4/4 technical scanner
Top-down market → sector → stock scanner
Relative strength comparison
Volume confirmation
RSI confirmation
EMA20 / EMA50 / SMA200 trend logic
ATR measurement
100-point setup-quality score
Master Selection
The final A+ candidate currently requires:
```text
Original scanner 4/4
Top-down scanner 4/4
SPY bullish
Sector bullish
Setup score >= 95
Volume >= 1.20x average
RSI 50-65
Stock outperforming sector
Sector outperforming SPY
Stock outperforming SPY
```
Current selection behavior:
```text
One current best candidate per completed scan
No candidate is valid when strict rules reject every stock
```
If no stock passes the rules:
```text
NO TRADE
```
The selector currently sets `weekly_lock` to `false`; candidates can change
between daily scans. It does not persist trade state or enforce a maximum of
one new trade per calendar week.
---
Risk Management
Current risk model:
```text
Account reference: $10,000
Maximum risk per trade: 1%
Stop: 1.5 × ATR
Target: 2R
Maximum holding period: 20 trading days
Typical historical holding period: about 5-15 trading days
```
Position sizing is calculated from the stop distance and maximum account risk.
---
Project Layout
```text
TradingBot/
│
├── README.md                  # Start here
├── .env                       # Local secrets; never share
├── .env.example               # Safe configuration template
├── .gitignore
├── Start TradingBot.bat       # Starts the app and scan pipeline
├── Backup TradingBot.bat      # Creates a private project backup
├── Install Daily Scan Task.bat
├── app.py                     # Webhook API and dashboard data
├── master_selector.py         # Applies the final strict rules
├── paper_journal.py           # Persistent paper-trade journal
├── project_paths.py           # Central paths for generated files
├── scan_notifications.py      # Optional SMTP scan-summary email
├── scanner.py                 # Original technical scan
├── scanner_topdown.py         # SPY → sector → stock scan
├── universe_builder.py        # Builds the daily stock universe
├── universe_loader.py         # Loads the generated universe
├── scheduled_daily_scan.ps1   # Compatibility entry for Task Scheduler
│
├── scripts/                   # Startup, schedule install, scan, and backup
├── web/                       # Read-only operator dashboard
├── tests/                     # Automated tests
├── docs/                      # Step-by-step project guide
├── infra/terraform/           # Modular Azure VM infrastructure as code
├── data/
│   ├── top_200_universe.json  # Current generated universe
│   ├── qualified_setups.json  # Current original scan
│   ├── qualified_setups_topdown.json
│   ├── master_candidates.json
│   ├── best_current_candidate.json
│   ├── paper_trades.sqlite3  # Persistent simulated trades
│   ├── paper_trades.csv      # Spreadsheet export of all paper trades
│   └── archive/root_snapshots/ # Previous root-level output copies
├── backtests/                 # Backtest programs
├── results/backtests/         # Backtest CSV results
├── logs/                      # Daily scan logs
├── requirements.txt
└── .venv/                     # Local Python environment; not backed up
```
Generated JSON and CSV results belong in `data/` or `results/`, not beside
the Python source files. The archived root snapshots are retained for
reference; current scripts do not read from that archive.
---
Step-by-Step Guide
For a guided setup, daily run, dashboard, result review, and troubleshooting
workflow, see [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md).

Start the Project
Recommended — Windows startup script
```powershell
.\Start TradingBot.bat
```
This starts the webhook API on `127.0.0.1:8000`, the local-only operator
dashboard on `127.0.0.1:8001`, the daily scan pipeline, and (if installed)
ngrok pointing only at port 8000. Open the dashboard at:
```text
http://127.0.0.1:8001
```
The dashboard is read-only, refreshes every 30 seconds, and has a manual data
refresh button that does not start a scan. It reports missing, invalid, or
stale scan artifacts as warnings rather than presenting them as healthy. The
paper journal separates open positions from closed history and summarizes
realized P&L and estimated risk to recorded stops; stops are not monitored or
executed automatically. It also keeps `data/paper_trades.csv` synchronized
with recorded entries and exits. Use **Download all trades (CSV)** to get the
complete journal in Excel-friendly format. SQLite remains the source of truth;
the generated CSV is ignored by Git.
The existing weekday scan is scheduled for 8:30 AM local time. If setting up
Task Scheduler on a new machine, run `Install Daily Scan Task.bat`. The task
runs only while your Windows user is signed in. For daily email summaries,
configure SMTP settings in `.env`; without them the scanner still runs and
logs that email was skipped.
Confirmed TradingView BUY/SELL webhooks open and close simulated paper-journal
records. The journal does not submit broker orders or auto-trigger stop/target
exits.

## DevOps and Infrastructure as Code

Start with source-control and secrets hygiene: this workspace is not currently
a Git repository, so initialize a private repository only after confirming
`.env` is ignored and rotating any secret that has been exposed. Then make
infrastructure reproducible before moving this local Windows/Python workflow
to the cloud. `infra/terraform/` contains modular Azure Terraform for a
Windows VM, restricted RDP access, private app ports, and an attached data
disk. It does not install or deploy the app, copy local data, or make the
scheduled task unattended. Follow its README to format, validate, and inspect
the plan; review ongoing VM costs and do not apply until you approve the plan.

Manual startup
Terminal 1 — Webhook API
Activate the virtual environment:
```powershell
.\.venv\Scripts\Activate.ps1
```
Start the API:
```powershell
uvicorn app:app --host 127.0.0.1 --port 8000
```
Terminal 2 — Dashboard
```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app:dashboard_app --host 127.0.0.1 --port 8001
```
Terminal 3 — Scanner / Testing
Activate the virtual environment:
```powershell
.\.venv\Scripts\Activate.ps1
```
Run the daily pipeline manually:
```powershell
python -u .\universe_builder.py
python -u .\scanner.py
python -u .\scanner_topdown.py
python -u .\master_selector.py
```
---
Terminal 4 — ngrok
Start the public HTTPS tunnel:
```powershell
ngrok http 8000
```
Do not forward the dashboard port. The local-only bind is not a substitute for
authentication if the dashboard is later deployed to a shared network.

Production-readiness limits
The current app is a local operator dashboard and webhook decision-support
service, not a multi-user production trading platform. It has no user accounts
or role-based access. The SQLite paper journal and duplicate-signal guard are
persistent, but its database is a local file, not a shared team ledger. The
dashboard must remain loopback-only unless proper authentication, HTTPS, and
network access controls are added. No live brokerage order placement is
implemented.
---
Automatic Daily Scan
Windows Task Scheduler runs the pipeline Monday-Friday at 8:30 AM Central time.
Install or refresh the task with:
```powershell
.\scripts\install_daily_task.ps1
```
The current Windows task is already registered; running this again may require
administrator permission to update its existing definition.
The scheduled scan implementation is:
```text
scripts/scheduled_daily_scan.ps1
```
The root-level `scheduled_daily_scan.ps1` remains as a compatibility wrapper
for existing Windows Task Scheduler registrations.
Execution order:
```text
1. universe_builder.py
2. scanner.py
3. scanner_topdown.py
4. master_selector.py
```
Run logs are stored in:
```text
logs/
```
---
Backtesting
Backtesting currently includes:
Original strategy testing
Score-band analysis
Sector analysis
Chronological portfolio simulation
Holdout-style testing
SPY benchmark comparison
Drawdown measurement
Profit factor
Expectancy
Holding-period analysis
Backtest code is stored in:
```text
backtests/
```
Backtest CSV output is stored in:
```text
results/backtests/
```
---
Current Development Goal
The next major goal is to backtest the exact final one-stock-per-week strategy:
```text
Dynamic 200-stock universe
+ bullish SPY
+ bullish sector
+ original scanner agreement
+ top-down scanner agreement
+ setup score >= 95
+ volume >= 1.20x
+ RSI 50-65
+ stock beats sector
+ sector beats SPY
+ stock beats SPY
+ maximum one new trade per week
```
Why
Earlier backtests tested older versions of the strategy.
The final master strategy is stricter and must be tested separately before relying on it.
What it will tell us
How many weekly opportunities appear
Win rate
Average winner / loser
Expectancy
Profit factor
Maximum drawdown
Consecutive losses
Average holding period
Weeks with no trade
Performance versus SPY
---
Next Development
After the final strategy backtest:
Forward paper-trade tracking
Persistent trade journal
Weekly performance reporting
Update `app.py` to use the weekly master selection
Docker containerization
Automated testing
Azure DevOps pipeline
Security scanning
Azure deployment
Monitoring and logging
Real-money brokerage automation should only be considered after sufficient forward paper-trading evidence.
