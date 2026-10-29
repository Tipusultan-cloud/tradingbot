# TradingBot — Project Log

A running record of everything done on this project: **what** was done,
**why** it was done, and **how** it works or was done. Newest entries go at the
top of the **Activity Log**. **Current State** is kept up to date as a snapshot.

> Never paste secrets (`.env` values, API keys, webhook secret, ngrok URL,
> SMTP app password) into this file.

---

## 1. The goal of the project

**What:** A swing-trading *decision-support* machine. Every weekday it scans
the market and either picks ONE high-quality "A+" stock setup or says
**NO TRADE / WAIT**.

**Why:** To trade less but better — only take trades when the overall market,
the stock's sector, and the stock itself all point up at the same time, with
fixed, small risk per trade. A bot that refuses to trade in weak markets is
safer than one that always finds something.

**How (in one line):** Download prices (yfinance) → build a 200-stock list →
run two independent scanners → a strict master selector must approve →
TradingView alerts are only accepted for the approved stock → trades are
recorded as **paper trades** (no real money, no broker orders).

---

## 2. What was built — what, why, how

### 2.1 Daily universe builder — `universe_builder.py`
- **What:** Creates the list of 200 stocks to watch each day →
  `data/top_200_universe.json` / `.csv`.
- **Why:** Scanning all ~500 S&P stocks is slow and noisy. Focusing on the
  strongest, most liquid stocks in the strongest sectors raises the quality of
  candidates.
- **How:**
  1. Loads the current S&P 500 member list.
  2. Ranks the 11 sectors using their sector ETFs vs SPY (1 year of data).
  3. Scores every stock: 20-day return (30%), 60-day return (15%), strength
     vs its sector (25%), dollar-volume liquidity (20%), +10 bonus for a strong
     trend.
  4. Takes a large share from the top 2 sectors and fills the rest from the
     other sectors (e.g. on 2026-10-21: 81 from Energy + Health Care, 119 from
     the rest = 200).
  5. Drops today's incomplete daily bar so half-finished data is never used.

### 2.2 Original scanner — `scanner.py`
- **What:** Checks each of the 200 stocks for a valid entry →
  `data/qualified_setups.json`.
- **Why:** To find stocks at a specific, historically tested entry point —
  not just "stocks going up".
- **How:**
  - **Market filter (SPY bullish):** Close > SMA200 **and** Close > EMA20
    **and** EMA20 > EMA50.
  - **Two entry patterns:**
    - `EMA20_RECLAIM` — uptrend (EMA20 > EMA50), yesterday closed at/below
      EMA20, today closes above it, RSI 50–65, volume ≥ 1.20× 20-day average.
    - `MOMENTUM_BREAKOUT` — Close > EMA20 > EMA50 > SMA200, close above the
      prior 20-day high, RSI 55–65, volume ≥ 1.50×.
    - If both match, it is labelled EMA20_RECLAIM (historically stronger).
  - **100-point setup score:** Trend 25 · Valid pattern 20 · RSI 10 ·
    Volume 15 · Strength vs SPY 15 · Market bullish 10 · ATR quality 5.
    Minimum to qualify: **95**.
  - "4/4" = trend, pattern, momentum (RSI), and volume checks all pass.

### 2.3 Top-down scanner — `scanner_topdown.py`
- **What:** A second, independent scan that goes **SPY → Sector → Stock** →
  `data/qualified_setups_topdown.json`.
- **Why:** A second opinion. A stock is only trusted if both scanners agree,
  which cuts false signals.
- **How:** Same indicator rules and score threshold, but it first requires SPY
  bullish, then the stock's sector ETF bullish, then checks the stock and its
  relative strength vs both the sector and SPY.

### 2.4 Master selector — `master_selector.py`
- **What:** Makes the final decision → `data/master_candidates.json` and
  `data/best_current_candidate.json` (status `SELECTED`, `WAIT`, or NO TRADE).
- **Why:** One clear answer per day instead of a long list; strict rules
  protect the account.
- **How:**
  - Refuses scanner files older than **72 hours** (fails closed).
  - A stock must pass ALL: original scan 4/4, top-down 4/4, SPY bullish,
    sector bullish, score ≥ 95, volume ≥ 1.20×, RSI 50–65, stock beats sector,
    sector beats SPY, stock beats SPY, **no bad news in the last 3 trading
    days** (`news_checker.py`). Earnings within 5 trading days
    shows a warning only (2026-10-21, `earnings_calendar.py`).
  - Builds a risk preview for the winner (see 2.7) and lists up to 5 alternates.
  - `weekly_lock` is currently `false` → the pick can change day to day.

### 2.5 Webhook API — `app.py` (port 8000)
- **What:** Receives TradingView BUY/SELL alerts over the internet via ngrok.
- **Why:** TradingView watches the chart in real time; the bot double-checks
  the alert before anything is recorded.
- **How:** Each alert goes through: secret check (`TV_WEBHOOK_SECRET` in
  `.env`) → ticker/action/price validation → duplicate protection (same signal
  within 60 s is rejected) → BUY only if the ticker is in **fresh** scanner
  output **and** is the current master pick (missing/stale data = reject) →
  risk calculation → paper-journal entry. SELL closes the paper position.

### 2.6 Dashboard — `app.py` `dashboard_app` + `web/` (port 8001)
- **What:** Read-only local web page at `http://127.0.0.1:8001`.
- **Why:** See market regime, latest pick, pipeline timestamps, and paper
  trades at a glance without opening JSON files.
- **How:** Reads the `data/` files, refreshes every 30 s, warns on
  missing/invalid/stale files. Bound to 127.0.0.1 only and **never** forwarded
  through ngrok because it has no login.

### 2.7 Risk management
- **What:** Position size, stop, and target for every candidate.
- **Why:** Small, fixed losses keep the account alive through losing streaks.
- **How:** $10,000 reference account · risk 1% ($100) per trade ·
  stop = entry − 1.5 × ATR · target = 2R (twice the risk) · shares =
  $100 ÷ stop distance · max hold 20 trading days.

### 2.8 Paper journal — `paper_journal.py`
- **What:** Records simulated trades in `data/paper_trades.sqlite3`, mirrored
  to `data/paper_trades.csv` (Excel-friendly; dashboard download button).
- **Why:** Prove the strategy forward in real time before any real money.
- **How:** Confirmed BUY → open position; SELL → close it and store realized
  P&L. Blocks a second open trade in the same ticker and duplicate signals.
  Since 2026-10-28 the daily scan closes paper trades at stop, 2R target or
  20 days (`paper_exits.py`). No broker orders.

### 2.9 Automation and scripts — `scripts/`, `.bat` files
- **What:** One-click start, scheduled daily scan, backups, optional email.
- **Why:** The scan must run every day without remembering to start it.
- **How:**
  - `Start TradingBot.bat` → `start_tradingbot.ps1`: starts API (8000),
    dashboard (8001), scan pipeline, and ngrok.
  - Windows Task Scheduler task **TradingBot Daily Scan**, Mon–Fri 8:30 AM,
    runs `scheduled_daily_scan.ps1` → the 4 stages in order, with a mutex so
    two scans never overlap; logs go to `logs/`.
  - `scan_notifications.py`: emails the result via SMTP (Gmail App Password)
    if configured in `.env`; skipped otherwise.
  - `Backup TradingBot.bat`: zip to `backups/`, excluding `.env` and `.venv`.

### 2.10 Backtesting — `backtests/` → `results/backtests/`
- **What:** Tested the strategy on 2019-10 → 2026-10 historical data.
- **Why:** Check that the rules made money in the past before trusting them,
  and check that we are not just fitting to old data.
- **How:** Simulated trades day by day with the same stop/target/risk rules,
  then measured win rate, expectancy, profit factor, drawdown, and compared to
  SPY. A **holdout** test kept 2024-10 → 2026-10 untouched during development
  and tested it only at the end.
- Versions in order: `backtester.py` (v1, all signals) → `backtester_v2.py`
  (score filters, older vs holdout) → `backtester_v3.py` (portfolio limits:
  max positions, sector limits, cash) → `benchmark_compare.py` (vs SPY,
  Sharpe/Sortino) → `backtest_dual_setup.py` (reclaim + breakout) →
  `backtest_strict_dual_setup.py` → `backtest_current_strategy.py` →
  `backtest_holdout_validation.py`.

### 2.11 Infrastructure as Code — `infra/terraform/`
- **What:** Modular Azure Terraform: resource group, network, Windows VM,
  data disk.
- **Why:** Prepare to move the bot off the home PC so it runs 24/7.
- **How:** RDP limited to one trusted IP, app ports kept private. **Not
  applied** — it costs money while running and does not yet install the app.

### 2.12 Tests — `tests/`
- **What:** `test_dashboard_data.py`, `test_paper_journal.py`,
  `test_scan_notifications.py`.
- **Why:** Catch breakage in the dashboard data, journal, and email logic.
- **How:** `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

---

## 3. Current State (as of 2026-10-21)

| Item | Value |
| --- | --- |
| Location | `C:\Users\tipus\OneDrive\Documents\TradingBot` |
| Remote | `https://github.com/Tipusultan-cloud/tradingbot.git` |
| Branch | `main` |
| Python env | `.venv/` (yfinance, FastAPI, uvicorn, alpaca-py 0.44.0, …) |
| Latest scan | 2026-10-21 08:33 — **WAIT**: SPY market regime is not bullish. 0 approved. |
| Top sectors today | Energy, Health Care |

### Backtest results (2019-10-07 → 2026-10-07 unless noted)
| Test | Trades | Win % | Expectancy (R) | Profit factor | Return % | Max DD % |
| --- | --- | --- | --- | --- | --- | --- |
| Original backtester (all signals) | 1953 | 39.8 | 0.105 | 1.18 | — | 101 R |
| Current strategy | 64 | 50.0 | 0.417 | 1.83 | 28.9 | 7.4 |
| Dual setup | 123 | 42.3 | 0.211 | 1.37 | 26.7 | 12.3 |
| Strict dual setup | 86 | 48.8 | 0.365 | 1.70 | 33.9 | 6.3 |
| Holdout — development (to 2024-10-04) | 60 | 46.7 | 0.280 | 1.53 | 16.8 | 6.3 |
| Holdout — untouched (2024-10-07 →) | 27 | 51.9 | 0.501 | 1.85 | 12.4 | 5.2 — **PASS** |
| SPY buy & hold, same period | — | — | — | — | 193.2 | 33.7 |

**What it means:** Each stricter version improved quality (win rate and
expectancy up, drawdown down), and the untouched holdout passed. But total
return is far below SPY because the bot is in the market only ~30–40% of the
time. The exact final one-trade-per-week strategy has **not** been backtested.

---

## 4. Open Items / Next Steps

- [ ] Backtest the exact final strategy (all A+ rules + max one new trade per
      week). *Why:* earlier backtests tested older versions.
- [x] News step 1: earnings-date blackout (2026-10-21).
- [x] News step 1b: backtest the earnings blackout (2026-10-21). It made
      results worse (return 33.9% → 17.7%). Changed to warning only.
- [x] News step 2: bad-news block built (2026-10-21).
- [ ] Add real Alpaca API keys to `.env`, then run
      `backtests/backtest_bad_news_block.py` and review the blocked headlines.
- [ ] Persistent one-trade-per-week lock (`weekly_lock` is always `false` now).
- [ ] Forward paper-trade tracking and weekly performance report.
- [ ] Update `app.py` to use the weekly master selection.
- [ ] Docker, CI pipeline (Azure DevOps), automated tests, security scanning.
- [ ] Review/apply Terraform (Azure VM) — only after approving plan and cost.
- [ ] Monitoring and logging for a cloud deployment.
- [x] Leader-pullback entry + 2 trade slots, paper only (2026-10-28).
- [x] Automatic paper exits at stop / 2R / 20 days (2026-10-28).
- [ ] After 2-3 months of paper trades, compare `A_PLUS` vs `PULLBACK`
      results in the journal against the backtest.
- [ ] Real-money automation — only after enough forward paper-trading evidence.

---

## 5. Activity Log (newest first)

### 2026-10-29 — Automatic trading on Alpaca paper + dashboard Buy buttons
- **What:** Optional automatic trading for Bot 1 and Bot 2 on an Alpaca
  **paper** account. New `broker.py` (Alpaca connection; refuses anything
  but paper), `trade_manager.py` (bracket orders, fill/exit sync, 20-session
  sell), `auto_trader.py` (buys slot picks after the scan; `--sync` records
  fills and exits). Dashboard: **Buy** button on every stock in the Bot 1 /
  Bot 2 lists (`POST /api/buy`) and an auto-trading status line. Journal:
  new `entry_source` (`AUTO` / `MANUAL`) and `broker_order_id` columns and a
  `broker_orders` table for orders waiting to fill.
- **Why:** User wants both bots to trade automatically and to cherry-pick
  stocks. User chose Alpaca paper (no money at risk, real stop orders) over
  journal-only or real money, and a dashboard Buy button over TradingView
  alerts for cherry-picks.
- **How:**
  - Off by default: needs `AUTO_TRADING=true`, `ALPACA_PAPER=true` and keys.
    No setting enables a live account.
  - Each buy is a GTC bracket order: market buy, stop at price - 1.5 ATR,
    take-profit at 2R, from Alpaca's latest trade price when sent. Shares
    risk 1% of `PAPER_ACCOUNT_BALANCE`. Orders only while the market is open.
  - Slots (`MAX_OPEN_TRADES`, 2) count open trades plus orders waiting to
    fill. A stock already held or waiting cannot be bought again.
  - Sync: filled entry → journal trade at the fill price; filled stop or
    target leg → closed `STOP` / `TARGET` at the fill; after 20 completed
    sessions (Alpaca calendar) the legs are canceled and the position sold
    at market (`TIME_EXIT`) at the next run during market hours.
  - Scan order: ... `paper_exits.py` → `auto_trader.py --sync` →
    `scanner_pullback.py` → `slot_selector.py` → `auto_trader.py`.
    `paper_exits.py` now skips Alpaca trades.
  - Buy endpoint safety: only from the local dashboard page (Origin must be
    127.0.0.1/localhost:8001 and a custom `X-TradingBot` header is required,
    which forces a CORS preflight the app never allows); the stock must be
    in the fresh Bot 1 / Bot 2 lists.
  - TradingView webhook alerts are ignored while auto trading is on, so the
    journal always matches the Alpaca account.
- **Files changed:** `broker.py`, `trade_manager.py`, `auto_trader.py`,
  `tests/test_auto_trading.py` (new); `app.py`, `paper_journal.py`,
  `paper_exits.py`, `slot_selector.py`, `web/dashboard.html`,
  `scripts/scheduled_daily_scan.ps1`, `scripts/start_tradingbot.ps1`,
  `.env.example`, `README.md`, `docs/PROJECT_GUIDE.md`, and older tests
  (now patch the auto-trading switch so `.env` cannot change them).
- **Result:** 189 tests pass (27 new, using a fake Alpaca client). Live:
  with the current `.env` (no keys, `AUTO_TRADING` unset) the auto trader
  skips and the dashboard shows "Auto trading OFF"; the Buy endpoint refuses
  requests without the dashboard headers (403) and cross-site preflights
  get 405. **Not yet tested against a real Alpaca paper account** (no keys).
- **Next:** User adds Alpaca paper keys and `AUTO_TRADING=true`, restarts,
  and checks the first orders in the Alpaca paper dashboard.

### 2026-10-29 — Bot 1 / Bot 2 lists and hand-picking
- **What:** Every scan now lists every stock from both strategies: **Bot 1 ·
  A+** (all approved A+ stocks) and **Bot 2 · Leader pullback** (up to 20,
  was 10), each with entry reference, stop, target, shares and flags for the
  bot's slot picks and held stocks. Shown on the dashboard (two side-by-side
  panels, each with its own paper results), in the email, and at the end of
  the scan window. `slot_selection.json` has a new `bots` field;
  `get_paper_trades()` returns `by_strategy` results.
- **Why:** User wants to see both bots' results every time the bot runs and
  sometimes pick good stocks themselves.
- **Rule change:** The webhook now confirms a BUY for **any** stock in either
  list, not only the bot's picks: any approved A+ stock (was: only the #1
  best) and any stock in the pullback list (was: only the slot picks). The
  2-slot limit, freshness checks and scanner check for A+ still apply.
  `test_master_candidate_rejects_non_selected_ticker` was replaced by tests
  for the new rule (approved accepted, unapproved rejected).
- **Files changed:** `slot_selector.py`, `scanner_pullback.py`, `app.py`,
  `paper_journal.py`, `scan_notifications.py`, `web/dashboard.html`,
  `tests/test_pullback_slots.py`, `tests/test_dashboard_data.py`,
  `README.md`, `docs/PROJECT_GUIDE.md`.
- **Result:** 162 tests pass. Live run 2026-10-29: Bot 1 = HCA; Bot 2 =
  MRNA, MPC, PSX, VLO, MRK; slot picks HCA and MRNA.
- **Note:** Hand-picked trades are not what the backtest tested (it always
  took the top pick); the per-bot results will include them.

### 2026-10-28 — Automatic paper exits at stop / 2R target / 20 days
- **What:** New `paper_exits.py`, run in the daily scan after
  `master_selector.py` and before `scanner_pullback.py` /
  `slot_selector.py`. It closes open paper trades that hit their stop, 2R
  target or 20-day limit, and records the reason (`STOP`, `GAP_STOP`,
  `TARGET`, `GAP_TARGET`, `TIME_EXIT`). New `paper_journal.close_trade()`
  closes one open trade by id. Dashboard: closed-trade history shows strategy
  and exit reason; open-positions note says exits are checked once a day.
- **Why:** User asked whether the bot has a stop loss. It calculated and
  recorded stops but never acted on them: paper trades only closed on a
  TradingView SELL, so a stopped-out trade kept its slot and blocked new
  picks, and paper results could not be compared with the backtests. User
  chose this option over per-trade TradingView exit alerts; real stop orders
  at a broker are still needed before real money.
- **How:** Reuses `shadow_exits.simulate("RULE_2R_20D", ...)` and
  `fetch_daily_bars`, so the live exit is the same rule as the backtests and
  the Exit comparison panel: recorded stop, 2R from entry, close of the 20th
  session; stop checked before target; gaps fill at the open; the entry
  day's high/low are skipped (entry time within the day is unknown); no
  slippage. Exit time is recorded as 4 PM New York on the exit day. A failed
  price lookup leaves the trade open and is retried next run. A TradingView
  SELL that arrives first still closes the trade as before.
- **Files changed:** `paper_exits.py`, `tests/test_paper_exits.py` (new);
  `paper_journal.py`, `web/dashboard.html`,
  `scripts/scheduled_daily_scan.ps1`, `scripts/start_tradingbot.ps1`,
  `README.md`, `docs/PROJECT_GUIDE.md`.
- **Result:** 156 tests pass (12 new). Live run: no open paper trades yet.
  Real-price check on a temporary journal: MPC (entry 2026-08-11 $318.29)
  closed at its 2R target $353.17 on 2026-08-13, as in the backtest. GDDY
  (entry 2026-09-22 $105.90) closed as GAP_STOP at $98.16 on 2026-09-23; the
  backtest stopped it on the entry day, which the live check skips.
- **Next:** With real money, place stop orders at the broker; this check runs
  once a day on paper trades only.

### 2026-10-28 — Leader-pullback entry and 2 trade slots (paper only)
- **What:** The bot can now hold up to 2 paper trades at once
  (`MAX_OPEN_TRADES`, default 2). Free slots are filled with the A+ pick
  first, then "leader pullbacks". New `scanner_pullback.py` →
  `data/pullback_setups.json`; new `slot_selector.py` →
  `data/slot_selection.json`; both run in the daily scan after
  `master_selector.py` (failures only warn). The webhook also confirms a BUY
  for a pullback slot pick, and rejects any BUY while all slots are full.
  The paper journal records the strategy of each trade (`A_PLUS` /
  `PULLBACK`; new `strategy` column, added automatically to the existing
  journal). Dashboard has a **Trade slots** panel and a strategy column on
  open positions; the email lists the slot picks.
- **Why:** User asked to improve returns. Backtests in this session showed
  the current bot is in cash most of the time: over the last 6 months it took
  9 trades and lost $88 on $7,000. Wider stops (2.0 / 2.5 ATR) did not help
  (2.5 ATR lost more in both 6 and 12 months), so the stop stays 1.5 ATR.
  Across ~850,000 S&P 500 stock-days (2019-2026), the bot's entry conditions
  did not raise the chance of a stock rising (56% after 20 days, same as any
  stock), while 6-month leaders that fell >5% in a week averaged +3.2% over
  20 days vs +1.4% for any stock. User chose to build the 2-slot combination.
- **How:**
  - Leader pullback: top 20% of the S&P 500 by 126-session return,
    Close > SMA200, EMA20 > EMA50, and close more than 5% below the close 5
    sessions earlier. Ranked by 6-month return. Always ranked against the
    S&P 500 (as backtested), whatever `UNIVERSE_SOURCE` is.
  - Same stop / target / size as A+ (1.5 ATR, 2R, 1% risk). No market
    filter. Earnings and news shown as warnings only; neither was in the
    pullback backtest.
  - `backtests/backtest_pullback_slots.py` (10 years, $7,000, 1% risk of
    total equity per trade, compounding, same execution as
    `backtest_holdout_validation.py`). In a same-data check its 1-slot
    results matched the holdout engine to within $3. Results in
    `results/backtests/pullback_slots_comparison.csv`, `_trades.csv`,
    `_yearly.csv`.
- **Results (10 years, 2016-10-28 → 2026-10-28, start $7,000):**

  | Variant | Trades | Win % | Exp. R | PF | Return | Max DD |
  |---|---|---|---|---|---|---|
  | 1 slot, A+ only (current bot) | 130 | 49.2 | 0.34 | 1.63 | +49% | 7.3% |
  | 1 slot, pullback only | 238 | 48.7 | 0.31 | 1.63 | +101% | 10.0% |
  | **2 slots, A+ then pullback (live)** | 462 | 49.1 | 0.30 | 1.69 | **+264%** | 12.2% |
  | 3 slots, A+ then pullback | 693 | 47.5 | 0.25 | 1.55 | +386% | 14.5% |

  2 slots beat the current bot in every period (first 5 years +$5,122 vs
  +$1,364; last 5 +$7,636 vs +$1,724; last 2 +$2,956 vs +$701; last 12
  months +$607 vs +$49). One losing year (2022, −$163). 3 slots made more
  but per-trade quality fell (0.20R, PF 1.33 in the first 5 years). A+
  trades get priority, but 84% of 2-slot trades were pullbacks (+0.31R vs
  +0.27R for A+). An earlier scratch run one day before gave +282% for 2
  slots; the 10-year window had shifted by one session.
- **Caveats:** Today's S&P 500 members are used historically, which flatters
  dip-buying. The pullback rule was chosen after looking at 2019-2026 data,
  so there is no untouched holdout. Paper-trade it before real money.
- **Note:** Pullback paper trades are only journaled when a TradingView (or
  manual) BUY alert arrives for the picked ticker.
- **Files changed:** `scanner_pullback.py`, `slot_selector.py`,
  `backtests/backtest_pullback_slots.py`, `tests/test_pullback_slots.py`
  (new); `app.py`, `paper_journal.py`, `project_paths.py`,
  `scan_notifications.py`, `web/dashboard.html`,
  `scripts/scheduled_daily_scan.ps1`, `scripts/start_tradingbot.ps1`,
  `.env.example`, `README.md`, `docs/PROJECT_GUIDE.md`.
- **Result:** 144 tests pass (28 new). Live run on 2026-10-28: 500 stocks
  ranked, 4 pullbacks (MRNA, MPC, PSX, VLO); slots picked MRNA and MPC (no
  A+ pick today).
- **Next:** Paper-trade both strategies for 2-3 months and compare
  `A_PLUS` vs `PULLBACK` results in the journal before any real money.

### 2026-10-27 — Backtest: S&P 500 + top-200 most-traded universe
- **What:** New `backtests/backtest_top200_universe.py`; results in
  `results/backtests/top200_universe_comparison.csv`, `_trades.csv`,
  `_members.csv`. Price download cached in
  `results/backtests/us_stock_prices_cache.pkl` (24 h, gitignored).
- **Why:** User asked to backtest the new `SP500_TOP200` universe.
- **How:** 10 years of prices for ~5,900 US stocks. On each day the 200
  non-S&P stocks with the highest trailing 20-day dollar volume (price >= $5,
  >= 201 bars, known sector) are added, so membership uses no future data;
  780 different stocks were added over 7 years, exactly 200 per day. The
  engine and rules are those of `backtest_holdout_validation.py`.
- **Results (start $10,000 each period):**

  | Period | Universe | Trades | Win % | Exp. R | Return | Max DD |
  |---|---|---|---|---|---|---|
  | Dev 2019-24 | S&P 500 | 62 | 45.2 | 0.25 | 15.8% | 5.5% |
  | Dev 2019-24 | + top 200 | 62 | 43.6 | 0.22 | 13.7% | 6.0% |
  | Holdout 2024-26 | S&P 500 | 27 | 51.9 | 0.40 | 10.1% | 5.3% |
  | Holdout 2024-26 | + top 200 | 36 | 36.1 | 0.03 | 1.8% | 6.8% |
  | Full 7 years | S&P 500 | 89 | 47.2 | 0.33 | 31.5% | 5.5% |
  | Full 7 years | + top 200 | 98 | 42.9 | 0.22 | 22.6% | 7.3% |

  The 38 trades on added stocks won 34% and averaged 0.00R: they replaced
  better S&P trades. Worse in both periods even though survivorship bias
  flatters the added stocks.
- **Bug found and fixed:** Yahoo refused many sector lookups (HTTP 401);
  yfinance returns empty info instead of raising, so `_yahoo_sector` cached
  those stocks as "no sector" forever. It now raises when Yahoo returns no
  data (`quoteType` missing), so the lookup is retried. The 658 "no sector"
  cache entries were removed to be looked up again (backup in the session
  scratchpad). The backtest still had 200 added stocks every day, but which
  200 may differ slightly once those sectors are known.
- **Decision:** User chose to keep `UNIVERSE_SOURCE=SP500_TOP200` for the
  live paper-trading scan despite the weaker backtest.

### 2026-10-27 — Russell 2000 removed; S&P 500 + 200 most-traded US stocks
- **What:** Removed the same-day `SP500_R2000` option (`russell2000.py`,
  its tests, `.env` line). Added `UNIVERSE_SOURCE=SP500_TOP200`, now set in
  `.env` for the regular scan.
- **Why:** User asked to drop the Russell 2000 and add the top 200 stocks
  alongside the S&P 500; chose "200 most traded" over biggest by market
  value or strongest movers.
- **How:** `build_sp500_top_traded_constituents` in `us_market_universe.py`
  downloads 1 year of prices for all US-listed common stocks, keeps liquid
  non-S&P stocks (price >= $5, 20-day $ volume >= $20M), ranks them by
  20-day average dollar volume, and takes the top 200 with a known sector
  (Yahoo lookup, cached). All S&P 500 members are kept. The daily
  200-stock selection and strategy rules are unchanged. Not backtested.

### 2026-10-27 — Regular scan uses S&P 500 + Russell 2000
- **What:** New `russell2000.py` and `UNIVERSE_SOURCE=SP500_R2000`, now set
  in `.env`, so the daily scan picks its 200 stocks from the S&P 500 plus
  the Russell 2000. Tests: `tests/test_russell2000.py`.
- **Why:** User asked for Russell 2000 and S&P 500 in the regular scan.
- **How:**
  - Russell 2000 members come from `data/russell2000_holdings.csv`, the
    iShares IWM holdings CSV, downloaded by hand: the automatic download is
    blocked by the iShares site's bot check. iShares gives each stock's
    GICS sector, so no sector lookups are needed.
  - S&P 500 members are always kept and keep their Wikipedia GICS sector
    where the lists overlap. Russell members need price >= $5 and 20-day
    average dollar volume >= $20M, same as US_ALL.
  - Warns once the file is over 120 days old; if the file is missing the
    scan falls back to the S&P 500 only, so the daily run never fails on it.
  - **Not backtested** on small caps; all strategy rules are unchanged.

### 2026-10-27 — Option to pick the universe from all US stocks
- **What:** New `us_market_universe.py`; `universe_builder.py` reads
  `UNIVERSE_SOURCE` from `.env`: `SP500` (default, unchanged) or `US_ALL`.
  Tests: `tests/test_us_market_universe.py`.
- **Why:** User wants to trade beyond the S&P 500.
- **How:**
  - Symbol lists from Nasdaq Trader (NASDAQ, NYSE, NYSE American, Arca,
    Cboe). Drops ETFs, test issues, warrants, units, rights, preferreds,
    notes: ~5,900 common stocks left (2026-10-26 list).
  - Downloads 1 year of prices in batches of 200 and keeps price >= $5 and
    20-day average dollar volume >= $20M.
  - Sector: S&P members keep GICS; others from Yahoo Finance, mapped to the
    11 GICS names, cached in `data/sector_cache.json` (1,458 cached on the
    first, cancelled run). Stocks without a sector are skipped.
  - The 200-stock selection and all strategy rules are unchanged.
  - **Not backtested on US_ALL** and not switched on. A first run to count
    today's candidates was cancelled by the user before finishing; the scan
    files it had changed were restored from a backup taken before the run.

### 2026-10-27 — Shadow exits on paper trades
- **What:** New `shadow_exits.py` (step 5 of the daily scan, failure only
  warns), `shadow_exits` table in the journal database, and an **Exit
  comparison** panel on the dashboard. Tests: `tests/test_shadow_exits.py`.
- **Why:** User chose to collect forward evidence for "half at 2R, trail the
  rest" (see the trend-exit backtest entry) before changing the live exit.
- **How:** Each paper trade is replayed on daily prices with `RULE_2R_20D`
  and `HALF_2R_TRAIL`, using the recorded entry and stop. Results are shown
  in R next to the TradingView exit actually taken. The entry day's
  high/low is skipped (time of entry unknown); stop fills first when a bar
  touches stop and target; no slippage. Nothing is sold automatically.
  The running API/dashboard must be restarted to show the new panel.

### 2026-10-27 — Trend-riding exit backtest (live exits unchanged)
- **What:** New `backtests/backtest_trend_exits.py` tests four "ride the
  trend" exits against the current 2R / 20-day exit on the same entries.
  Results: `results/backtests/trend_exits_comparison.csv` and
  `trend_exits_trades.csv`. Tests: `tests/test_trend_exits.py` (11).
- **Why:** User asked how to ride trends. The current exit caps every winner
  near 2R; the question was whether letting winners run pays off.
- **How:**
  - A copy of the base trade loop with a pluggable exit. All variants keep the
    1.5 ATR starting stop and 1% risk; trailing stops move up only, after the
    close, so they apply from the next day. The script stops with an error if
    the current exit does not reproduce `backtest_holdout_validation.py`
    trade for trade (check passed).
  - Results, full period 2019-10 → 2026-10 (SPY +178.9%):

    | Exit | Trades | Win % | Exp. R | Return | Max DD |
    |---|---|---|---|---|---|
    | Current 2R / 20 days | 90 | 46.7 | 0.31 | 30.6% | 6.4% |
    | Trail 3 ATR | 44 | 45.5 | 1.20 | 61.4% | 10.3% |
    | EMA20 close | 65 | 36.9 | 0.12 | 5.9% | 11.4% |
    | Half at 2R, trail rest | 44 | 45.5 | 0.86 | 41.3% | 7.1% |
    | 2R, hold >20d above EMA20 | 86 | 45.4 | 0.33 | 30.7% | 6.4% |
  - **Decision: keep the current exit.** The 3 ATR trail's gain depends on
    two trades (SKYD +25.4R, MSI +14.0R = 75% of its total R); without them
    it trails the current exit (13.4R vs 20.7R). It also did worse in the
    2-year holdout (+6.0% vs +10.1%), and survivorship bias (today's S&P list)
    flatters trend exits. EMA20 exit is clearly worse; the extended hold makes
    no difference.
  - Candidate for later: run "half at 2R, trail the rest" as a shadow record
    in the paper journal to collect forward evidence.

### 2026-10-23 — Tests for the master selector rules
- **What:** New `tests/test_master_selector.py` (31 tests) covering the
  strict A+ rules in `master_selector.py`.
- **Why:** The selector decides which stock is picked, but only its news and
  earnings handling had tests. A changed threshold (e.g. RSI 50-65 becoming
  50-56) could have slipped through unnoticed.
- **How:**
  - Each test writes fake scanner files to a temp folder, runs
    `master_selector.main()` against them with news/earnings lookups stubbed,
    and checks `master_candidates.json` / `best_current_candidate.json`. No
    change to `master_selector.py` was needed.
  - Covered: both scanners must agree (ticker, setup type, valid setup; the
    lower score is used); SPY / stock market flag / sector trend; every
    threshold just inside and just outside its limit (score 95, reclaim
    volume 1.20x and RSI 50-65, breakout volume 1.50x and RSI 55-65, stock >
    sector, sector > SPY, stock > SPY); news/earnings looked up only for
    technical passes; ranking order; 72-hour freshness check; risk preview
    math (1% risk, 1.5x ATR stop, 2R target, cash cap).
  - Checked that the tests catch rule changes: altering RSI max, minimum
    score, ATR multiplier, risk %, max age, or breakout volume in memory each
    made at least one test fail.
  - Run all tests: `.venv\Scripts\python.exe -m unittest discover -s tests`
    (75 tests, all passing).

### 2026-10-23 — README cleanup
- **What:** Fixed outdated parts of `README.md` and converted it to proper
  Markdown (headings, bullet lists).
- **Why:** The README said the project was not a Git repository, its layout
  left out `news_checker.py`, `earnings_calendar.py`, and `PROJECT_LOG.md`, it
  listed the paper journal and automated tests as future work, and it gave
  the scan time as both "local" and "Central". Plain-line headings also
  rendered as one block of text on GitHub.
- **How:** Documentation only, no code changes. The scan time now reads
  "8:30 AM in the computer's local time zone", which matches
  `scripts/install_daily_task.ps1`. The no-bad-news rule was added to the
  target strategy for the final backtest. The DevOps section moved below
  Backtesting.

### 2026-10-21 — Added bad-news block (news step 2)
- **What:** The master selector now **rejects** an A+ stock if any of its
  news headlines from the last 3 trading days reports a negative event. The
  news status (`CLEAR`, `BAD_NEWS`, `UNKNOWN`) and the bad headline are shown
  in the selector output, dashboard, email, and `master_candidates.json`.
- **Why:** User decision: "we want bad news blocked trade". A stock can still
  look technically perfect right after bad news (downgrade, offering,
  lawsuit), and that is the risk this rule targets.
- **How:**
  - New `news_checker.py`: fetches headlines from the **Alpaca News API**
    (Benzinga feed, free account) from midnight New York time 3 weekdays back
    up to scan time.
  - Matches headlines against keyword groups: downgrade, earnings miss,
    guidance or dividend cut, lawsuit / SEC / DOJ probe, share offering /
    dilution, bankruptcy / recall / CEO or CFO exit / FDA rejection / data
    breach / short report / restatement, and sharp-drop words (plunge, tumble).
  - Ignores law-firm "shareholder alert" press releases (issued for almost
    every stock that has dropped) and articles tagged with more than 3
    tickers (market round-ups).
  - Headline keywords only, so it is coarse: it can miss bad news worded
    differently and occasionally flag a harmless headline.
  - Missing keys or a failed lookup → `UNKNOWN`: shown as a warning, not a
    block, so an outage cannot stop the scan.
  - Only runs for stocks that already passed every technical rule.
  - New `backtests/backtest_bad_news_block.py`: same backtest as the
    earnings one, but checks news for each daily best candidate at the
    entry-day open and takes the next-best stock when blocked. Results are
    cached in `results/backtests/news_checks_cache.csv`. It also reports how
    baseline trades taken *despite* bad news performed.
- **Blocker found:** `ALPACA_API_KEY` / `ALPACA_API_SECRET` in `.env` are
  placeholders, not real keys (Alpaca returns 401). Until real keys are added
  the news check reports `UNKNOWN` and the backtest cannot run.
- **Files changed:** `news_checker.py` (new), `master_selector.py`,
  `scan_notifications.py`, `web/dashboard.html`, `tests/test_news_checker.py`
  (new), `tests/test_earnings_calendar.py`,
  `backtests/backtest_bad_news_block.py` (new), `.env.example`, `README.md`.
- **Result:** 44/44 tests pass (headline classification, law-firm and
  round-up filtering, weekend lookback, failure → UNKNOWN, selector rejects a
  bad-news stock).
- **Next:** Add real Alpaca keys → run the backtest → review blocked
  headlines for false positives → keep or adjust.

### 2026-10-21 — Earnings check changed from block to warning only
- **What:** The master selector no longer rejects stocks with earnings within
  5 trading days. It still looks up the date and shows `EARNINGS_SOON` (yellow),
  `CLEAR`, or `UNKNOWN` on the dashboard, in the email, and in
  `data/master_candidates.json`.
- **Why:** The backtest (entry below) showed the hard block lowered the 7-year
  return from 33.9% to 17.7% and nearly doubled drawdown. The agreed plan was
  to keep a rule only if the data supports it. A warning keeps the
  information so you can decide per trade, without the cost.
- **How:**
  - `earnings_calendar.py`: status `BLOCKED` renamed to `EARNINGS_SOON`;
    `EARNINGS_BLACKOUT_TRADING_DAYS` renamed to `EARNINGS_WARNING_TRADING_DAYS`.
  - `master_selector.py`: removed the rejection; the result is attached to the
    candidate only. Rules field is now `earnings_warning_trading_days`.
  - `backtests/backtest_earnings_blackout.py` still tests the hard-block
    version, so it can be rerun later with more data.
  - Tests updated: an `EARNINGS_SOON` stock is now approved with the warning
    attached.
- **Files changed:** `earnings_calendar.py`, `master_selector.py`,
  `backtests/backtest_earnings_blackout.py`, `tests/test_earnings_calendar.py`,
  `README.md`, `PROJECT_LOG.md`.
- **Result:** All tests pass; selector runs on today's data (WAIT, SPY not
  bullish).
- **Next:** News step 2, the bad-news block, must also be backtested before it
  is kept.

### 2026-10-21 — Backtested the earnings blackout: it made results worse
- **What:** Tested the new earnings rule on 7 years of history
  (2019-10-21 → 2026-10-21), comparing the strategy with and without it.
- **Why:** The plan was to keep each news rule only if a backtest shows it
  helps. A rule that only "sounds safe" adds complexity and can cost money.
- **How:**
  - New `backtests/backtest_earnings_blackout.py` reuses the validated holdout
    backtest (same data, periods, entries, stops, slippage) and runs it twice.
  - Yahoo Finance earnings history for all 503 S&P stocks (back to 2014),
    cached in `results/backtests/earnings_dates_cache.csv`.
  - When the best stock is blocked, the next-best A+ stock is taken, the same
    as the live selector. Added an optional `exclude_tickers` setting to
    `get_daily_best_candidate` in `backtest_holdout_validation.py`; it is off by
    default, so that backtest's results do not change.
  - Sanity check: baseline holdout = 27 trades, +0.501R, the same as the
    earlier holdout run.
- **Result:**

  | Period | Strategy | Trades | Win % | Expectancy R | PF | Return % | Max DD % |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | Development | Baseline | 62 | 46.8 | 0.287 | 1.57 | 18.5 | 5.9 |
  | Development | Earnings block | 59 | 42.4 | 0.102 | 1.18 | 5.7 | 10.3 |
  | Holdout | Baseline | 27 | 51.9 | 0.501 | 1.85 | 12.4 | 5.2 |
  | Holdout | Earnings block | 26 | 53.9 | 0.487 | 1.86 | 11.6 | 5.2 |
  | Full 7 yrs | Baseline | 89 | 48.3 | 0.352 | 1.68 | 33.9 | 5.9 |
  | Full 7 yrs | Earnings block | 85 | 45.9 | 0.220 | 1.39 | 17.7 | 10.3 |

  - Only 17 signal days in 7 years were blocked.
  - Trades that held through earnings: 11 trades, 7 winners, avg **+1.1R**,
    against +0.2 to +0.4R for all other trades. Strong A+ momentum stocks
    tended to gap **up** on earnings (ALB, DHI hit target on the gap). The
    worst earnings gap down was CDNS at −1.29R, close to a normal stop.
  - Blocking them swapped in weaker trades, so return roughly halved and
    drawdown nearly doubled.
- **Caution:** 11 trades is a very small sample. This does not prove earnings
  are good to hold; it shows the block is not supported by the data.
- **Decision:** Changed to warning only (see the entry above).
- **Files changed:** `backtests/backtest_earnings_blackout.py` (new),
  `backtests/backtest_holdout_validation.py` (optional parameter only);
  results in `results/backtests/earnings_blackout_*.csv`,
  `earnings_blocked_signals.csv`, `earnings_dates_cache.csv`.

### 2026-10-21 — Added earnings-date blackout (news filter, step 1)
- **What:** The master selector now rejects any A+ stock that has an earnings
  report within the next **5 trading days**. Earnings status is shown in the
  selector output, `data/master_candidates.json`, the dashboard, and the
  daily email.
- **Why:** Trades last 5–15 days. An earnings report can gap a stock 10–20%
  overnight, straight past the 1.5×ATR stop, so the stop gives no
  protection. Avoiding earnings is the highest-value "news" rule and the
  first step of the news plan (1. earnings block → 2. bad-news block →
  3. backtest both → 4. positive-news bonus only if proven).
- **How:**
  - New `earnings_calendar.py`: gets past and scheduled earnings dates from
    Yahoo Finance (`yfinance` `get_earnings_dates`), takes the next one on or
    after today (New York date), and counts weekdays until it.
    0–5 days → `BLOCKED`; more → `CLEAR`; lookup error or no scheduled date
    → `UNKNOWN`.
  - `UNKNOWN` does **not** block; it shows a yellow warning. This matches the
    plan: a data outage should not stop the scan, but you check the date
    yourself before acting.
  - `master_selector.py` only checks earnings for stocks that already passed
    every technical rule (1–5 stocks, so only a few network calls). A
    blocked stock moves to `rejected` with the reason, so the webhook also
    refuses a TradingView BUY for it automatically.
  - Market holidays are not excluded from the day count, so around a
    holiday the window can be one trading day shorter.
- **Files changed:** `earnings_calendar.py` (new), `master_selector.py`,
  `scan_notifications.py`, `web/dashboard.html`, `README.md`,
  `tests/test_earnings_calendar.py` (new), `requirements.txt`.
- **Also fixed:** `requirements.txt` was missing `yfinance`, `pandas`,
  `numpy`, and `alpaca-py`, which the scanners use, so a fresh install
  (new PC or Azure) would have failed. Added them at the installed versions
  and saved the file as UTF-8.
- **Result:**
  - Tests: 32/32 pass. They include a test that sends a fake A+ stock through
    the real selector and confirms the earnings block rejects it.
  - A test caught a bug (it assumed the dates came back in order); fixed by
    using the earliest upcoming date.
  - Live check: AAPL next earnings 2026-11-02 (8 days, CLEAR); XOM 2026-10-30
    (7 days, CLEAR); a fake ticker gives UNKNOWN.
  - Selector run on today's data: WAIT (SPY not bullish), no errors.
- **Next:** Backtest the earnings block against the holdout results (yfinance
  has earnings history back many years). Then step 2, the bad-news block.

### 2026-10-21 — Expanded project log with what / why / how
- **What:** Rewrote `PROJECT_LOG.md` to explain every component and step.
- **Why:** User asked for a record of what was done, why, and how.
- **How:** Read `README.md`, `docs/PROJECT_GUIDE.md`, the scanner / selector /
  universe source code, backtest summaries, and the latest scan log.
- **Files changed:** `PROJECT_LOG.md`.

### 2026-10-21 — Project log created
- **What:** Created `PROJECT_LOG.md`.
- **Why:** Keep one place with the full history of the project.
- **How:** Gathered state from git history, `results/backtests/`, and `logs/`.

### 2026-10-21 08:32–08:33 — Daily scan
- **What:** Daily pipeline ran (logs at 08:32:06 and 08:33:04).
- **Why:** Scheduled weekday scan.
- **How:** Task Scheduler → `scheduled_daily_scan.ps1` → universe → scanner →
  top-down → master selector → email (if configured).
- **Result:** Universe = 200 (Energy, Health Care top). Decision **WAIT** —
  SPY not bullish, so no stock can be approved. This is the rules working as
  designed.

### 2026-10-20 23:53 — Put the project in Git and linked to GitHub
- **What:** Commit `a42a973` "Add TradingBot project" (all code, backtests,
  results, Terraform, docs, scripts); commit `1606f63` merged the existing
  GitHub repo history (`a3fbf5c` Initial commit).
- **Why:** Version history, backup, and the first DevOps step.
- **How:** `.gitignore` set up first so `.env`, `.venv/`, `logs/`, SQLite
  journal, `backups/`, and Terraform state/tfvars are never committed.

### 2026-10-05 → 2026-10-20 — Daily scans running
- **What:** Scans logged on 10-05, 10-06, 10-07, 10-09, 10-13, 10-14, 10-15,
  10-16, 10-18, 10-19, 10-20 (`logs/`).
- **Why/How:** Same scheduled pipeline as above.

### Before 2026-10-20 — Project built
- **What / Why / How:** See section 2 for each component: universe builder,
  two scanners, master selector, webhook API, dashboard, risk model, paper
  journal, scripts and scheduling, backtests v1 → holdout validation,
  Terraform, and tests.

---

## Entry template

```markdown
### YYYY-MM-DD HH:MM — Short title
- **What:** what was done
- **Why:** the reason / goal
- **How:** steps, commands, or logic used
- **Files changed:** list
- **Result:** outcome, numbers, test results
- **Next:** follow-up actions
```
