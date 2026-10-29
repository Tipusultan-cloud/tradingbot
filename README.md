# TradingBot

trade-machine

## Purpose

TradingBot is a swing-trading decision-support project. The system combines:

- TradingView alerts
- FastAPI webhook processing
- Secret validation
- Duplicate protection
- Dynamic stock-universe building
- Technical stock scanning
- Market and sector confirmation
- News and earnings checks
- Risk-based position sizing
- Backtesting
- Weekly master selection

The goal is to fill up to two paper-trade slots: the A+ swing candidate
first, then "leader pullbacks" (strong stocks that just dipped). A
persistent one-trade-per-week lock is not implemented yet.

The system does not guarantee a winning trade and does not automatically place
a real-money trade.

---

## Current Project Flow

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
        │
        │      scanner_pullback.py
        │      (S&P 500 leaders down >5% this week)
        │             │
        └──────┬──────┘
               ↓
        slot_selector.py
               ↓
  Fill free slots (max 2 open):
  A+ pick first, then pullbacks
               ↓
  data/slot_selection.json
```

---

## TradingView → FastAPI Flow

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
output and is an approved A+ stock in the master selection (Bot 1 list), or
is in the fresh leader-pullback list (Bot 2 list). Either way, a BUY is
rejected while all `MAX_OPEN_TRADES` slots (default 2) hold open paper
trades. A missing or stale result fails closed. SELL signals continue to be
handled as exit alerts.

---

## Current Features

### Webhook / API

- FastAPI webhook server
- TradingView BUY / SELL signal support
- Webhook secret stored in `.env`
- Invalid secret rejection
- Ticker / action / price validation
- Duplicate signal protection
- ngrok public HTTPS tunnel

### Market Scanning

- Dynamic source universe: S&P 500 (default) or all liquid US-listed
  common stocks (`UNIVERSE_SOURCE=US_ALL` in `.env`), or the S&P 500 plus the 200
  most-traded other US stocks (`SP500_TOP200`)
- 200-stock daily monitoring universe
- Sector ranking
- Market trend confirmation using SPY
- Sector trend confirmation using sector ETFs
- Original 4/4 technical scanner
- Top-down market → sector → stock scanner
- Relative strength comparison
- Volume confirmation
- RSI confirmation
- EMA20 / EMA50 / SMA200 trend logic
- ATR measurement
- 100-point setup-quality score

### Master Selection

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
No bad news in the last 3 trading days
```

**Bad-news block:** `news_checker.py` reads the stock's recent headlines from
the Alpaca News API and rejects the stock when one reports a negative event
(downgrade, earnings miss, guidance cut, lawsuit or probe, share offering,
bankruptcy, recall, executive exit, sharp drop). Law-firm "investor alert"
releases and multi-ticker market round-ups are ignored. It needs
`ALPACA_API_KEY` and `ALPACA_API_SECRET` in `.env` (free Alpaca account);
without them, or if the lookup fails, the stock is marked `UNKNOWN` and not
blocked. Backtest: `backtests/backtest_bad_news_block.py`.

**Earnings warning (not a rule):** `earnings_calendar.py` looks up the
selected stock's next earnings date from Yahoo Finance and marks it
`EARNINGS_SOON` when it is within 5 trading days, `CLEAR` otherwise, or
`UNKNOWN` if the lookup fails. The status appears on the dashboard and in the
email; review the gap risk before acting. It does not reject the stock: a
backtest of a hard block (`backtests/backtest_earnings_blackout.py`) lowered
the 7-year return from 33.9% to 17.7%.

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

### Trade Slots and Leader Pullbacks (paper only)

Up to `MAX_OPEN_TRADES` paper trades (default 2) can be open at once. Each
open trade in the paper journal uses one slot. After each scan,
`slot_selector.py` fills the free slots in this order:

1. The A+ candidate above, if there is one.
2. Leader pullbacks from `scanner_pullback.py`, strongest first.

Stocks already held are skipped. A **leader pullback** is an S&P 500 stock
that is:

```text
In the top 20% of the S&P 500 by 6-month return
Above its 200-day average, with EMA20 > EMA50
Down more than 5% over the last 5 sessions
```

Stop, target and position size are the same as for A+ trades (1.5 × ATR,
2R, 1% risk). Pullbacks use no market filter. Earnings and news are shown as
warnings, not blocks, because neither was in the pullback backtest. The
pullback ranking always uses the S&P 500, whatever `UNIVERSE_SOURCE` is.

Picks are in `data/slot_selection.json`, the dashboard's **Trade slots**
panel, and the email.

**Both bots' full lists:** every scan also lists every stock from each
strategy so you can pick by hand: **Bot 1 · A+** (every stock passing all A+
rules) and **Bot 2 · Leader pullback** (up to 20 pullbacks), each with entry
reference, stop, target and shares, and with the bot's own picks and held
stocks flagged. The lists are shown on the dashboard (with each bot's paper
results: trades, open, win rate, realized P&L), in the email, and at the end
of the scan window. A BUY alert is confirmed for any stock in either list
while a slot is free. Paper journal entries record which strategy opened
them (`A_PLUS` or `PULLBACK`).

Backtest (`backtests/backtest_pullback_slots.py`, 10 years, $7,000 start):
2 slots made +264% against +49% for the current one-trade bot, with a 12.2%
max drawdown against 7.3%. It uses today's S&P 500 members, which flatters
dip-buying, and the rule was chosen after looking at recent data. Forward
paper results should confirm it before real money is used.

---

## Risk Management

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

**Automatic paper exits:** each daily scan runs `paper_exits.py` before the
slot selector. It replays every open paper trade on completed daily bars and
closes it at the recorded stop, at 2R, or at the close of the 20th session,
the same rule as the backtests. The stop is checked before the target; a gap
through either fills at the open; the entry day's high and low are skipped.
The exit reason (`STOP`, `GAP_STOP`, `TARGET`, `GAP_TARGET`, `TIME_EXIT`) is
saved in the journal, and the freed slot can be refilled the same morning.
This is a once-a-day check of paper trades, not real-time protection: with
real money, place stop orders at the broker. A TradingView SELL that arrives
first still closes the trade (`TRADINGVIEW_SELL`).

**Shadow exits:** after each daily scan, `shadow_exits.py` replays every
paper trade on daily prices with two exit rules: *2R or 20 days* and *half
at 2R, trail the rest 3 ATR below the highest close*. The dashboard's **Exit
comparison** panel shows both next to the TradingView exit actually taken.
Nothing is sold automatically; this collects forward evidence before any
exit rule is changed (backtest: `backtests/backtest_trend_exits.py`).

### Universe source

`UNIVERSE_SOURCE` in `.env` picks the stocks the daily 200 are chosen from:

- `SP500` (default): S&P 500 members, the universe all backtests used.
- `SP500_TOP200`: S&P 500 members plus the 200 most-traded other US common
  stocks (highest 20-day average dollar volume, same filters and sector
  lookup as `US_ALL` below). Each run downloads prices for all US-listed
  stocks to rank them, so the universe step takes several minutes.- `US_ALL`: every US-listed common stock (NASDAQ, NYSE, NYSE American,
  Arca, Cboe) from the Nasdaq Trader symbol lists, excluding ETFs,
  warrants, units, rights, preferreds and notes, then filtered to price
  >= $5 and 20-day average dollar volume >= $20M. Sectors for non-S&P
  stocks are looked up on Yahoo Finance once and cached in
  `data/sector_cache.json`; stocks without a sector are skipped. The
  first run takes much longer while sectors are looked up.

The strategy has not been backtested on `US_ALL`.

---

## Project Layout

```text
TradingBot/
│
├── README.md                  # Start here
├── PROJECT_LOG.md             # What was done, why, and how (newest first)
├── .env                       # Local secrets; never share
├── .env.example               # Safe configuration template
├── .gitignore
├── Start TradingBot.bat       # Starts the app and scan pipeline
├── Backup TradingBot.bat      # Creates a private project backup
├── Install Daily Scan Task.bat
├── app.py                     # Webhook API and dashboard data
├── earnings_calendar.py       # Next-earnings-date warning
├── master_selector.py         # Applies the final strict rules
├── news_checker.py            # Bad-news headline block (Alpaca News API)
├── paper_exits.py             # Daily stop / 2R / 20-day paper exits
├── paper_journal.py           # Persistent paper-trade journal
├── project_paths.py           # Central paths for generated files
├── scan_notifications.py      # Optional SMTP scan-summary email
├── scanner.py                 # Original technical scan
├── scanner_pullback.py        # Leader-pullback scan (S&P 500)
├── scanner_topdown.py         # SPY → sector → stock scan
├── shadow_exits.py            # Replays exit rules on paper trades
├── slot_selector.py           # Fills free trade slots: A+, then pullbacks
├── universe_builder.py        # Builds the daily stock universe
├── us_market_universe.py      # US-stock sources (SP500_TOP200, US_ALL)├── universe_loader.py         # Loads the generated universe
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
│   ├── pullback_setups.json   # Current leader pullbacks
│   ├── slot_selection.json    # Stocks picked for free trade slots
│   ├── paper_trades.sqlite3   # Persistent simulated trades
│   ├── paper_trades.csv       # Spreadsheet export of all paper trades
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

## Step-by-Step Guide

For a guided setup, daily run, dashboard, result review, and troubleshooting
workflow, see [docs/PROJECT_GUIDE.md](docs/PROJECT_GUIDE.md).

## Start the Project

### Recommended — Windows startup script

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
realized P&L and estimated risk to recorded stops; stops and targets are
checked once a day by the scan (see Automatic paper exits). It also keeps `data/paper_trades.csv` synchronized
with recorded entries and exits. Use **Download all trades (CSV)** to get the
complete journal in Excel-friendly format. SQLite remains the source of truth;
the generated CSV is ignored by Git.

The weekday scan is scheduled for 8:30 AM in the computer's local time zone
(see [Automatic Daily Scan](#automatic-daily-scan)). For daily email
summaries, configure SMTP settings in `.env`; without them the scanner still
runs and logs that email was skipped.

Confirmed TradingView BUY/SELL webhooks open and close simulated paper-journal
records. The daily scan also closes paper trades at stop, 2R target or 20
days. The journal does not submit broker orders.

### Manual startup

**Terminal 1 — Webhook API**

Activate the virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Start the API:

```powershell
uvicorn app:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — Dashboard**

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app:dashboard_app --host 127.0.0.1 --port 8001
```

**Terminal 3 — Scanner / Testing**

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
python -u .\paper_exits.py
python -u .\scanner_pullback.py
python -u .\slot_selector.py
```

**Terminal 4 — ngrok**

Start the public HTTPS tunnel:

```powershell
ngrok http 8000
```

Do not forward the dashboard port. The local-only bind is not a substitute for
authentication if the dashboard is later deployed to a shared network.

### Production-readiness limits

The current app is a local operator dashboard and webhook decision-support
service, not a multi-user production trading platform. It has no user accounts
or role-based access. The SQLite paper journal and duplicate-signal guard are
persistent, but its database is a local file, not a shared team ledger. The
dashboard must remain loopback-only unless proper authentication, HTTPS, and
network access controls are added. No live brokerage order placement is
implemented.

---

## Automatic Daily Scan

Windows Task Scheduler runs the pipeline Monday-Friday at 8:30 AM in the
computer's local time zone. The task runs only while your Windows user is
signed in. Install or refresh the task with `Install Daily Scan Task.bat` or:

```powershell
.\scripts\install_daily_task.ps1
```

If the task is already registered, running this again may require
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
5. paper_exits.py (a failure only warns)
6. scanner_pullback.py (a failure only warns)
7. slot_selector.py (a failure only warns)
8. shadow_exits.py (a failure only warns)
```

Run logs are stored in `logs/`.

---

## Backtesting

Backtesting currently includes:

- Original strategy testing
- Score-band analysis
- Sector analysis
- Chronological portfolio simulation
- Holdout-style testing
- SPY benchmark comparison
- Drawdown measurement
- Profit factor
- Expectancy
- Holding-period analysis
- Bad-news block comparison
- Earnings blackout comparison
- Leader-pullback entry and 1-3 trade slots

Backtest code is stored in `backtests/`. Backtest CSV output is stored in
`results/backtests/`.

---

## DevOps and Infrastructure as Code

The project is a Git repository. Before pushing, confirm `.env` is ignored and
rotate any secret that has ever been exposed. Make infrastructure reproducible
before moving this local Windows/Python workflow to the cloud.
`infra/terraform/` contains modular Azure Terraform for a Windows VM,
restricted RDP access, private app ports, and an attached data disk. It does
not install or deploy the app, copy local data, or make the scheduled task
unattended. Follow its README to format, validate, and inspect the plan;
review ongoing VM costs and do not apply until you approve the plan.

---

## Current Development Goal

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
+ no bad news in the last 3 trading days
+ maximum one new trade per week
```

### Why

Earlier backtests tested older versions of the strategy. The final master
strategy is stricter and must be tested separately before relying on it.

### What it will tell us

- How many weekly opportunities appear
- Win rate
- Average winner / loser
- Expectancy
- Profit factor
- Maximum drawdown
- Consecutive losses
- Average holding period
- Weeks with no trade
- Performance versus SPY

---

## Next Development

Already in place: the persistent paper-trade journal (`paper_journal.py`) and
automated tests (`tests/`). After the final strategy backtest:

- Forward paper-trade tracking (collect evidence with the existing journal)
- Weekly performance reporting
- Persistent one-trade-per-week lock
- Update `app.py` to use the weekly master selection
- Docker containerization
- Azure DevOps pipeline (run the tests on every change)
- Security scanning
- Azure deployment
- Monitoring and logging

Real-money brokerage automation should only be considered after sufficient
forward paper-trading evidence.
