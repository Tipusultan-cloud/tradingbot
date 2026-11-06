"""Compare buy times on the backtest entry days: daily bars for 10 years, Alpaca
IEX 5-minute bars since 2020-09. Run backtest_three_bots.py first.
"""
import os, pickle, sys, time
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backtests")]
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
t = pd.read_csv(ROOT / "results/backtests/three_bots_trades.csv", parse_dates=["entry_date"])
t = t[["ticker", "variant", "entry_date"]]
names = {"Bot 1 A+ Hunter": "Bot 1", "Bot 2 Dip Buyer": "Bot 2", "Bot 3 Momentum Rider": "Bot 3"}
t["bot"] = t.variant.map(names)
with open(ROOT / "results/backtests/three_bots_prices.pkl", "rb") as f:
    frames = pickle.load(f)
rows = []
for r in t.itertuples():
    f = frames[r.ticker].dropna(subset=["Close"]); f.index = pd.to_datetime(f.index).normalize()
    if r.entry_date not in f.index: continue
    i = f.index.get_loc(r.entry_date)
    if i == 0: continue
    rows.append({"bot": r.bot, "gap": (f.Open.iloc[i] / f.Close.iloc[i-1] - 1) * 100,
                 "open_to_close": (f.Close.iloc[i] / f.Open.iloc[i] - 1) * 100})
d = pd.DataFrame(rows); del frames
print("DAILY, 10 years, entry day (%):")
print(d.groupby("bot").agg(n=("gap", "size"), gap_mean=("gap", "mean"), open_to_close_mean=("open_to_close", "mean"), open_to_close_median=("open_to_close", "median")).round(2).to_string())
c = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY").strip(), os.getenv("ALPACA_API_SECRET").strip())
marks = ["09:35", "09:45", "10:00", "10:30", "11:00", "12:00", "13:00", "14:00", "15:00", "15:55"]
recent = t[t.entry_date >= "2020-09-01"]
out, fails = [], 0
for r in recent.itertuples():
    day = r.entry_date.tz_localize("America/New_York")
    try:
        b = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=r.ticker, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=(day + pd.Timedelta(hours=9, minutes=30)).to_pydatetime(), end=(day + pd.Timedelta(hours=16)).to_pydatetime(), feed=DataFeed.IEX)).df
    except Exception as e:
        fails += 1; last_err = str(e)[:120]; continue
    if b.empty: fails += 1; continue
    b = b.droplevel(0); b.index = b.index.tz_convert("America/New_York")
    if len(b) < 20: fails += 1; continue
    o = b.open.iloc[0]
    row = {"bot": r.bot}
    for m in marks:
        cut = day + pd.Timedelta(hours=int(m[:2]), minutes=int(m[3:]))
        seen = b[b.index < cut]
        row[m] = (seen.close.iloc[-1] / o - 1) * 100 if len(seen) else np.nan
    row["low_time"] = b.low.idxmin().strftime("%H:%M")
    out.append(row)
print("\nintraday trades:", len(out), "skipped:", fails, (last_err if fails and 'last_err' in dir() else ""))
x = pd.DataFrame(out)
x.to_csv(ROOT / "results/backtests/buy_time.csv", index=False)
pd.set_option("display.width", 200, "display.max_columns", 30)
print("\nAverage price vs the open (%), lower = cheaper to buy:")
print(x.groupby("bot")[marks].mean().round(2).to_string()); print("ALL  ", x[marks].mean().round(2).to_dict())
print("\nMedian:"); print(x.groupby("bot")[marks].median().round(2).to_string())
se = x[marks].sub(x["09:45"], axis=0)
print("\nvs 09:45 for all: mean diff / std error"); print(pd.DataFrame({"mean": se.mean(), "se": se.std() / np.sqrt(se.count())}).round(3).T.to_string())
print("\nday's low by hour:", x.low_time.str[:2].value_counts().sort_index().to_dict())
