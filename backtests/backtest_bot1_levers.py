"""Which changes would make Bot 1 earn more? Reuses three_bots_prices.pkl from
backtest_three_bots.py (run that first). Same limitations as that test; the
SPY parking rows are approximate. Saves results/backtests/bot1_levers.csv.
"""
import pickle
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backtests")]

import backtest_three_bots as three

bp, hv = three.bp, three.hv

with three.PRICE_CACHE.open("rb") as f:
    frames = pickle.load(f)
cons = hv.load_sp500_constituents()
sp500 = set(cons["ticker"])
market = hv.download_many(["SPY", *hv.SECTOR_ETFS.values()], "market")
spy = hv.add_indicators(market["SPY"])
sectors = {e: hv.add_indicators(market[e]) for e in hv.SECTOR_ETFS.values() if e in market}
cal = spy.index
last = cal.max()
full = cal[cal >= last - pd.DateOffset(years=10)]
sig = full[:-1]
raw = {t: frames[t].dropna(subset=["Close"]).astype("float64") for t in sp500 if t in frames}
del frames
dfs, panel = hv.prepare_stock_data(cons, raw)
panel = panel[panel["AVG_VOLUME20"] > 0]

a95 = bp.build_a_plus_candidates(panel, spy, sectors, sig)
hv.RECLAIM_MIN_SETUP_SCORE = hv.BREAKOUT_MIN_SETUP_SCORE = 90
a90 = bp.build_a_plus_candidates(panel, spy, sectors, sig)
hv.RECLAIM_MIN_SETUP_SCORE = hv.BREAKOUT_MIN_SETUP_SCORE = 95
pull = bp.build_pullback_candidates(panel, set(sig))
print("signals 95:", len(a95), "90:", len(a90))
earn = three.load_earnings()

periods = {
    "full": full,
    "first5": full[full < last - pd.DateOffset(years=5)],
    "last5": full[full >= last - pd.DateOffset(years=5)],
}
spy_ret = spy["Close"].pct_change()


def parked(trades, equity, share=0.5):
    """Approximate: `share` of idle cash earns SPY's daily return."""
    invested = pd.Series(0.0, index=equity.index)
    for t in trades.itertuples():
        invested.loc[t.entry_date:t.exit_date] += t.entry_price * t.shares
    base = equity.pct_change().fillna(0)
    idle = (1 - invested / equity).clip(lower=0)
    extra = idle.shift(1).fillna(1) * share * spy_ret.reindex(equity.index).fillna(0)
    return three.STARTING_EQUITY * (1 + base + extra).cumprod()


def line(name, lookup, slots=1, earnings=earn, risk=1.0, target=2.5, partial=2.0, park=False):
    hv.RISK_PER_TRADE_PCT = risk
    three.TARGET_R, three.PARTIAL_R = target, partial
    out = {"variant": name}
    for period, days in periods.items():
        trades, equity, exposure = three.simulate(lookup, days, dfs, slots, earnings)
        if park:
            equity = parked(trades, equity)
        s = three.summarize(trades, equity, exposure)
        out[f"{period}_ret"] = s["total_return_pct"]
        out[f"{period}_dd"] = s["max_drawdown_pct"]
        if period == "full":
            out.update(trades=s["trades"], cagr=s["cagr_pct"], expR=s.get("expectancy_r"),
                       pf=s.get("profit_factor"), expo=s["exposure_pct"])
    hv.RISK_PER_TRADE_PCT, three.TARGET_R, three.PARTIAL_R = 1.0, 2.5, 2.0
    return out


L = bp.ranked(a95)
trail = bp.ranked(a95.assign(source="MOMENTUM"))
rows = [
    line("BASE Bot 1 live rules", L),
    line("risk 1.5%", L, risk=1.5),
    line("risk 2%", L, risk=2.0),
    line("2 slots", L, slots=2),
    line("3 slots", L, slots=3),
    line("score >= 90", bp.ranked(a90)),
    line("score >= 90, 2 slots", bp.ranked(a90), slots=2),
    line("target 3R (half 2R)", L, target=3.0),
    line("target 4R (half 2R)", L, target=4.0),
    line("half 1.5R, rest 2.5R", L, partial=1.5),
    line("3 ATR trail, no target", trail, earnings=None),
    line("no earnings rules", L, earnings=None),
    line("BASE + 50% idle in SPY (approx)", L, park=True),
    line("Bot 1 then Bot 2 fills, 1 slot", bp.ranked(a95, pull)),
    line("Bot 1 then Bot 2 fills, 2 slots (live)", bp.ranked(a95, pull), slots=2),
    line("live 2 slots + 50% idle in SPY (approx)", bp.ranked(a95, pull), slots=2, park=True),
]
table = pd.DataFrame(rows)
with pd.option_context("display.width", 220, "display.max_columns", 30):
    print(table.to_string(index=False))
table.to_csv(three.BACKTEST_RESULTS_DIR / "bot1_levers.csv", index=False)
