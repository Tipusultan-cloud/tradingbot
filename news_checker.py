"""Bad-news block for swing-trade candidates.

Looks at the candidate's news headlines from the last few trading days
(Alpaca News API, Benzinga feed) and blocks the trade when a headline
reports a clearly negative event: analyst downgrade, earnings miss,
guidance cut, lawsuit or regulator probe, share offering, bankruptcy,
recall, executive exit, and similar.

Matching is by keyword on the headline only, so it is a coarse filter.
Law-firm "investor alert" press releases are ignored because they are
issued for almost every stock that has ever dropped. Articles tagged with
many tickers (market round-ups) are ignored because the headline is
rarely about this stock.

A failed lookup or missing API keys is reported as UNKNOWN (a warning,
not a block) so an outage does not stop the scan.
"""

import os
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np

NEWS_LOOKBACK_TRADING_DAYS = 3
MAX_SYMBOLS_PER_ARTICLE = 3
NEWS_PAGE_LIMIT = 50
MARKET_TIMEZONE = ZoneInfo("America/New_York")

STATUS_CLEAR = "CLEAR"
STATUS_BAD_NEWS = "BAD_NEWS"
STATUS_UNKNOWN = "UNKNOWN"

NEGATIVE_PATTERNS = {
    "downgrade": [
        r"\bdowngrade[sd]?\b",
        r"\bcuts? (?:\w+ )?to (?:sell|underperform|underweight|reduce)\b",
    ],
    "earnings_miss": [
        r"\bmiss(?:es|ed)\b",
        r"\b(?:below|short of|lags?) (?:estimates?|expectations|consensus)\b",
    ],
    "guidance_cut": [
        r"\b(?:cuts?|lowers?|lowered|reduces?|slash(?:es)?|withdraws?)\b"
        r".{0,30}\b(?:guidance|outlook|forecast)\b",
        r"\bweak (?:guidance|outlook|forecast)\b",
        r"\b(?:cuts?|suspends?|eliminates?) (?:its |the )?dividend\b",
    ],
    "legal_regulatory": [
        r"\b(?:sues|sued|lawsuit|class action)\b",
        r"\b(?:sec|doj|ftc|justice department)\b"
        r".{0,40}\b(?:probe|investigat\w*|charges?|subpoena\w*)\b",
        r"\b(?:probe|subpoena\w*|fraud|indicted|indictment)\b",
    ],
    "offering_dilution": [
        r"\b(?:secondary|public|stock|share|equity) offering\b",
        r"\bdilut\w*\b",
    ],
    "business_event": [
        r"\brecalls?\b",
        r"\b(?:bankrupt\w*|chapter 11|delist\w*)\b",
        r"\b(?:ceo|cfo)\b.{0,30}\b(?:resigns?|steps? down|departs?|ousted|fired)\b",
        r"\bfda\b.{0,40}\b(?:rejects?|declines?)\b",
        r"\bcomplete response letter\b",
        r"\b(?:data breach|cyberattack)\b",
        r"\bshort[- ]seller\b|\bshort report\b",
        r"\b(?:restate\w*|accounting irregularit\w*)\b",
        r"\b(?:halts?|halted|suspends?) (?:production|operations|shipments)\b",
    ],
    "price_drop": [
        r"\b(?:plunges?|plummets?|tumbles?|craters?|nosedives?)\b",
    ],
}

# Law-firm solicitations ("investor alert", "lead plaintiff deadline").
IGNORE_PATTERNS = [
    r"\blaw firm\b",
    r"\b(?:shareholder|investor) alert\b",
    r"\binvestors? who (?:lost|purchased|bought)\b",
    r"\b(?:reminds?|encourages?|notifies) (?:investors|shareholders)\b",
    r"\blead plaintiff\b",
    r"\bon behalf of (?:investors|shareholders)\b",
    r"\b(?:rosen|pomerantz|levi & korsinsky|bragar|faruqi|bronstein|glancy|"
    r"kessler topaz|schall|gross law|portnoy|robbins llp|kirby mcinerney|"
    r"bernstein liebhard|hagens berman|block & leviton)\b",
]

_NEGATIVE = {
    category: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for category, patterns in NEGATIVE_PATTERNS.items()
}
_IGNORE = [re.compile(pattern, re.IGNORECASE) for pattern in IGNORE_PATTERNS]


def classify_headline(headline):
    """Negative category for a headline, or None."""
    if not headline or any(pattern.search(headline) for pattern in _IGNORE):
        return None
    for category, patterns in _NEGATIVE.items():
        if any(pattern.search(headline) for pattern in patterns):
            return category
    return None


def lookback_start(as_of, lookback_days=NEWS_LOOKBACK_TRADING_DAYS):
    """Midnight (New York) lookback_days weekdays before as_of."""
    as_of_local = as_of.astimezone(MARKET_TIMEZONE)
    start_day = np.busday_offset(
        as_of_local.date(), -lookback_days, roll="backward"
    ).astype(object)
    return datetime.combine(start_day, time.min, tzinfo=MARKET_TIMEZONE)


def _alpaca_keys():
    from dotenv import load_dotenv

    from project_paths import BASE_DIR

    load_dotenv(BASE_DIR / ".env")
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_API_SECRET", "").strip()
    if not key or not secret or key.upper().startswith("YOUR"):
        raise RuntimeError(
            "Alpaca API keys are not configured in .env "
            "(ALPACA_API_KEY / ALPACA_API_SECRET)."
        )
    return key, secret


def fetch_news(ticker, start, end):
    """Headlines for one ticker between start and end (Alpaca News API)."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    key, secret = _alpaca_keys()
    client = NewsClient(key, secret)
    response = client.get_news(NewsRequest(
        symbols=ticker,
        start=start,
        end=end,
        limit=NEWS_PAGE_LIMIT,
    ))
    return [
        {
            "headline": item.headline,
            "created_at": item.created_at,
            "symbols": list(item.symbols or []),
            "source": item.source,
            "url": item.url,
        }
        for item in response.data.get("news", [])
    ]


def check_news(
    ticker,
    as_of=None,
    lookback_days=NEWS_LOOKBACK_TRADING_DAYS,
    fetch=fetch_news,
):
    as_of = as_of or datetime.now(MARKET_TIMEZONE)
    start = lookback_start(as_of, lookback_days)
    result = {
        "status": STATUS_UNKNOWN,
        "lookback_trading_days": lookback_days,
        "articles_checked": 0,
        "bad_headlines": [],
        "detail": "",
    }

    try:
        articles = fetch(ticker, start, as_of)
    except Exception as exc:  # network, auth, and API errors
        result["detail"] = f"News lookup failed: {exc}"
        return result

    relevant = [
        article
        for article in articles
        if len(article.get("symbols") or [ticker]) <= MAX_SYMBOLS_PER_ARTICLE
    ]
    result["articles_checked"] = len(relevant)

    for article in relevant:
        category = classify_headline(article.get("headline"))
        if category:
            created_at = article.get("created_at")
            result["bad_headlines"].append({
                "category": category,
                "headline": article.get("headline"),
                "created_at": (
                    created_at.isoformat()
                    if isinstance(created_at, datetime)
                    else created_at
                ),
                "url": article.get("url"),
            })

    if result["bad_headlines"]:
        first = result["bad_headlines"][0]
        result["status"] = STATUS_BAD_NEWS
        result["detail"] = (
            f"Bad news ({first['category']}): {first['headline']}"
        )
    else:
        result["status"] = STATUS_CLEAR
        result["detail"] = (
            f"No bad news in {len(relevant)} article(s) over the last "
            f"{lookback_days} trading days."
        )
    return result
