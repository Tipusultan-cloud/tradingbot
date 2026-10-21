from pathlib import Path
import math
import pandas as pd
import yfinance as yf

EQUITY_FILES = [
    ("V3 Holdout 90+", "v3_holdout_90plus_equity.csv"),
    ("V3 Older 90+", "v3_older_90plus_equity.csv"),
]

BENCHMARK_TICKER = "SPY"
RISK_FREE_RATE = 0.0


def max_drawdown_pct(equity):
    peak = equity.cummax()
    dd = (equity / peak - 1) * 100
    return abs(float(dd.min()))


def annualized_volatility_pct(returns):
    if len(returns) < 2:
        return 0.0
    return float(returns.std() * math.sqrt(252) * 100)


def sharpe_ratio(returns):
    if len(returns) < 2:
        return 0.0
    std = returns.std()
    if std == 0 or pd.isna(std):
        return 0.0
    daily_rf = (1 + RISK_FREE_RATE) ** (1 / 252) - 1
    excess = returns - daily_rf
    return float(excess.mean() / excess.std() * math.sqrt(252))


def sortino_ratio(returns):
    if len(returns) < 2:
        return 0.0
    downside = returns[returns < 0]
    if downside.empty:
        return math.inf
    downside_std = downside.std()
    if downside_std == 0 or pd.isna(downside_std):
        return 0.0
    daily_rf = (1 + RISK_FREE_RATE) ** (1 / 252) - 1
    return float(
        (returns.mean() - daily_rf)
        / downside_std
        * math.sqrt(252)
    )


def calculate_metrics(dates, values):
    idx = pd.to_datetime(dates)
    equity = pd.Series(values.values, index=idx).astype(float)
    equity = equity[~equity.index.duplicated(keep="last")].sort_index()

    start_value = float(equity.iloc[0])
    end_value = float(equity.iloc[-1])
    total_return = (end_value / start_value - 1) * 100

    days = max((equity.index[-1] - equity.index[0]).days, 1)
    years = days / 365.25
    cagr = ((end_value / start_value) ** (1 / years) - 1) * 100

    daily_equity = equity.resample("D").ffill()
    daily_returns = daily_equity.pct_change().dropna()

    max_dd = max_drawdown_pct(daily_equity)
    vol = annualized_volatility_pct(daily_returns)
    sharpe = sharpe_ratio(daily_returns)
    sortino = sortino_ratio(daily_returns)
    calmar = cagr / max_dd if max_dd > 0 else math.inf

    return {
        "start_date": equity.index[0].date(),
        "end_date": equity.index[-1].date(),
        "starting_equity": round(start_value, 2),
        "ending_equity": round(end_value, 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "annualized_volatility_pct": round(vol, 2),
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3) if math.isfinite(sortino) else "INF",
        "calmar_ratio": round(calmar, 3) if math.isfinite(calmar) else "INF",
    }


def load_strategy_equity(path):
    df = pd.read_csv(path)
    if not {"date", "equity"}.issubset(df.columns):
        raise RuntimeError(f"{path.name} needs date and equity columns.")
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["date", "equity"]).sort_values("date")


def load_spy_equity(start_date, end_date, starting_equity):
    requested_end = pd.Timestamp(end_date) + pd.Timedelta(days=1)
    spy = yf.Ticker(BENCHMARK_TICKER).history(
        start=pd.Timestamp(start_date).strftime("%Y-%m-%d"),
        end=requested_end.strftime("%Y-%m-%d"),
        auto_adjust=True,
    )
    if spy.empty:
        raise RuntimeError("Could not download SPY data.")

    spy.index = pd.to_datetime(spy.index)
    if getattr(spy.index, "tz", None) is not None:
        spy.index = spy.index.tz_localize(None)

    first_close = float(spy["Close"].iloc[0])
    equity = spy["Close"] / first_close * starting_equity

    return pd.DataFrame({
        "date": spy.index,
        "equity": equity.values,
    })


def print_block(title, metrics):
    print(title)
    print("Ending Equity:", f"${metrics['ending_equity']}")
    print("Total Return:", f"{metrics['total_return_pct']}%")
    print("CAGR:", f"{metrics['cagr_pct']}%")
    print("Max Drawdown:", f"{metrics['max_drawdown_pct']}%")
    print("Annual Volatility:", f"{metrics['annualized_volatility_pct']}%")
    print("Sharpe:", metrics["sharpe_ratio"])
    print("Sortino:", metrics["sortino_ratio"])
    print("Calmar:", metrics["calmar_ratio"])


def main():
    project_dir = Path(__file__).parent
    rows = []
    found = False

    print()
    print("================================")
    print("TRADINGBOT VS SPY BENCHMARK")
    print("================================")

    for label, filename in EQUITY_FILES:
        path = project_dir / filename

        if not path.exists():
            print()
            print(f"Skipping {label}: {filename} not found.")
            continue

        found = True
        strategy = load_strategy_equity(path)
        strategy_metrics = calculate_metrics(
            strategy["date"],
            strategy["equity"],
        )

        spy = load_spy_equity(
            strategy_metrics["start_date"],
            strategy_metrics["end_date"],
            strategy_metrics["starting_equity"],
        )
        spy_metrics = calculate_metrics(
            spy["date"],
            spy["equity"],
        )

        print()
        print("================================")
        print(label)
        print("================================")
        print(
            "Period:",
            strategy_metrics["start_date"],
            "to",
            strategy_metrics["end_date"],
        )
        print()
        print_block("TRADINGBOT", strategy_metrics)
        print()
        print_block("SPY BUY & HOLD", spy_metrics)

        rows.append({"period": label, "asset": "TradingBot", **strategy_metrics})
        rows.append({"period": label, "asset": "SPY", **spy_metrics})

    if not found:
        raise RuntimeError(
            "No V3 equity files found. Run backtester_v3.py first."
        )

    output = project_dir / "benchmark_comparison.csv"
    pd.DataFrame(rows).to_csv(output, index=False)

    print()
    print("================================")
    print("BENCHMARK FILE CREATED")
    print("================================")
    print(output)
    print()
    print("Compare return, CAGR, drawdown, volatility, Sharpe, Sortino and Calmar together.")
    print("Sharpe/Sortino use a 0% risk-free rate in this first version.")


if __name__ == "__main__":
    main()
