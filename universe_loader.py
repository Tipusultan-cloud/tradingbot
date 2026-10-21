import json

from project_paths import TOP_200_JSON


def load_top_200_universe():
    """
    Load the dynamic 200-stock universe created by universe_builder.py.

    Returns:
        sectors: dict[str, list[str]]
        ticker_to_sector: dict[str, str]
        tickers: list[str]
    """

    if not TOP_200_JSON.exists():
        raise FileNotFoundError(
            f"{TOP_200_JSON} was not found. "
            "Run universe_builder.py first."
        )

    with TOP_200_JSON.open(
        "r",
        encoding="utf-8"
    ) as file:
        payload = json.load(file)

    stocks = payload.get(
        "stocks",
        []
    )

    if not stocks:
        raise RuntimeError(
            "top_200_universe.json contains no stocks."
        )

    sectors = {}
    ticker_to_sector = {}

    for stock in stocks:
        ticker = stock.get(
            "ticker"
        )

        sector = stock.get(
            "sector"
        )

        if not ticker or not sector:
            continue

        sectors.setdefault(
            sector,
            []
        ).append(
            ticker
        )

        ticker_to_sector[
            ticker
        ] = sector

    tickers = list(
        ticker_to_sector.keys()
    )

    if not tickers:
        raise RuntimeError(
            "No valid tickers were loaded from the universe file."
        )

    return (
        sectors,
        ticker_to_sector,
        tickers
    )
