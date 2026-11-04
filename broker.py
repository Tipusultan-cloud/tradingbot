"""Alpaca connection for automatic trading. Paper trading only.

Auto trading is on only when .env has AUTO_TRADING=true, ALPACA_PAPER=true
and Alpaca API keys (a free Alpaca paper account). A live (real-money)
account is refused: there is no setting that enables one.
"""

import os

from dotenv import load_dotenv

from project_paths import BASE_DIR


def auto_trading_status():
    """(enabled, reason) from .env, without contacting Alpaca."""
    load_dotenv(BASE_DIR / ".env")
    if os.getenv("AUTO_TRADING", "false").strip().lower() != "true":
        return False, "Auto trading is off (set AUTO_TRADING=true in .env)."
    if os.getenv("ALPACA_PAPER", "true").strip().lower() != "true":
        return False, "Only Alpaca paper trading is supported (ALPACA_PAPER=true)."
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_API_SECRET", "").strip()
    if not key or not secret or key.upper().startswith("YOUR"):
        return False, "Alpaca API keys are missing in .env."
    return True, "Alpaca paper trading"


ALL_BOTS = ("A_PLUS", "PULLBACK", "MOMENTUM")


def auto_buy_bots():
    """Bots whose slot picks the morning run buys (AUTO_BUY_BOTS in .env,
    comma-separated A_PLUS, PULLBACK, MOMENTUM). Unset = all three; the
    others are bought by hand from the dashboard."""
    load_dotenv(BASE_DIR / ".env")
    value = os.getenv("AUTO_BUY_BOTS", "").strip()
    if not value:
        return list(ALL_BOTS)
    bots = [item.strip().upper() for item in value.split(",") if item.strip()]
    unknown = [bot for bot in bots if bot not in ALL_BOTS]
    if unknown:
        raise RuntimeError(
            f"AUTO_BUY_BOTS has unknown bot(s) {', '.join(unknown)}; "
            f"use {', '.join(ALL_BOTS)}"
        )
    return [bot for bot in ALL_BOTS if bot in bots]


def get_trading_client():
    enabled, reason = auto_trading_status()
    if not enabled:
        raise RuntimeError(reason)

    from alpaca.trading.client import TradingClient

    return TradingClient(
        os.environ["ALPACA_API_KEY"].strip(),
        os.environ["ALPACA_API_SECRET"].strip(),
        paper=True,
    )


def latest_price(ticker):
    """Last traded price from Alpaca market data."""
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"].strip(),
        os.environ["ALPACA_API_SECRET"].strip(),
    )
    trades = client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=ticker)
    )
    return float(trades[ticker].price)


def latest_prices(tickers):
    """{ticker: last traded price} for many tickers in one request."""
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    if not tickers:
        return {}
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"].strip(),
        os.environ["ALPACA_API_SECRET"].strip(),
    )
    trades = client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=sorted(set(tickers)))
    )
    return {ticker: float(trade.price) for ticker, trade in trades.items()}
