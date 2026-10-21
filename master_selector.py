import json
import math
from datetime import datetime, timezone

from project_paths import (
    DATA_DIR,
    QUALIFIED_SETUPS_JSON,
    QUALIFIED_TOPDOWN_JSON,
    MASTER_CANDIDATES_JSON,
)

MIN_SETUP_SCORE = 95

RECLAIM_MIN_VOLUME_RATIO = 1.20
RECLAIM_MIN_RSI = 50
RECLAIM_MAX_RSI = 65

BREAKOUT_MIN_VOLUME_RATIO = 1.50
BREAKOUT_MIN_RSI = 55
BREAKOUT_MAX_RSI = 65

REQUIRE_STOCK_BEATS_SECTOR = True
REQUIRE_SECTOR_BEATS_SPY = True
REQUIRE_STOCK_BEATS_SPY = True

MAX_SCANNER_AGE_HOURS = 72

ACCOUNT_BALANCE = 10000.0
RISK_PER_TRADE_PCT = 1.0
ATR_MULTIPLIER = 1.5
REWARD_RISK_RATIO = 2.0

TOP_ALTERNATES_TO_SHOW = 5

BEST_CURRENT_CANDIDATE_JSON = (
    DATA_DIR / "best_current_candidate.json"
)


def load_json(path):
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def parse_timestamp(value):
    if not value:
        return None

    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def ensure_fresh(payload, name):
    generated = parse_timestamp(
        payload.get("generated_at")
    )

    if generated is None:
        raise RuntimeError(
            f"{name} has no generated_at timestamp."
        )

    age = (
        datetime.now(timezone.utc) - generated
    ).total_seconds() / 3600

    if age > MAX_SCANNER_AGE_HOURS:
        raise RuntimeError(
            f"{name} is stale: {age:.1f} hours old. "
            "Run the scanner pipeline again."
        )


def build_risk_preview(price, atr):
    if price <= 0 or atr <= 0:
        return None

    max_risk = (
        ACCOUNT_BALANCE
        * (RISK_PER_TRADE_PCT / 100)
    )

    stop_distance = atr * ATR_MULTIPLIER
    stop_price = price - stop_distance

    if stop_price <= 0:
        return None

    target_price = (
        price
        + stop_distance * REWARD_RISK_RATIO
    )

    shares_by_risk = math.floor(
        max_risk / stop_distance
    )

    shares_by_cash = math.floor(
        ACCOUNT_BALANCE / price
    )

    shares = min(
        shares_by_risk,
        shares_by_cash
    )

    planned_risk = shares * stop_distance
    position_value = shares * price

    return {
        "entry_reference": round(price, 2),
        "atr": round(atr, 2),
        "stop": round(stop_price, 2),
        "target": round(target_price, 2),
        "shares": int(shares),
        "position_value": round(position_value, 2),
        "max_risk": round(max_risk, 2),
        "planned_risk": round(planned_risk, 2),
        "reward_risk": REWARD_RISK_RATIO,
    }


def main():
    print()
    print("================================")
    print("STRICT DUAL MASTER SELECTOR")
    print("================================")
    print()

    base = load_json(QUALIFIED_SETUPS_JSON)
    topdown = load_json(QUALIFIED_TOPDOWN_JSON)

    ensure_fresh(
        base,
        "qualified_setups.json"
    )

    ensure_fresh(
        topdown,
        "qualified_setups_topdown.json"
    )

    market_bullish = bool(
        topdown.get(
            "market",
            {}
        ).get(
            "bullish",
            False
        )
    )

    print(
        "Market / SPY:",
        "BULLISH" if market_bullish else "NOT BULLISH"
    )

    base_qualified = {
        stock["ticker"]: stock
        for stock in base.get("qualified", [])
    }

    topdown_qualified = {
        stock["ticker"]: stock
        for stock in topdown.get("qualified", [])
    }

    common_tickers = sorted(
        set(base_qualified.keys())
        & set(topdown_qualified.keys())
    )

    print(
        "Base scanner A+ candidates:",
        len(base_qualified)
    )

    print(
        "Top-down A+ candidates:",
        len(topdown_qualified)
    )

    print(
        "Both scanners agree:",
        len(common_tickers)
    )

    approved = []
    rejected = []

    if market_bullish:
        for ticker in common_tickers:
            base_stock = base_qualified[ticker]
            top_stock = topdown_qualified[ticker]

            reasons = []

            base_setup_type = base_stock.get(
                "setup_type"
            )

            top_setup_type = top_stock.get(
                "setup_type"
            )

            setup_type = top_setup_type

            if (
                base_setup_type
                != top_setup_type
            ):
                reasons.append(
                    "Scanners disagree on setup type"
                )

            if setup_type not in {
                "EMA20_RECLAIM",
                "MOMENTUM_BREAKOUT"
            }:
                reasons.append(
                    "Unknown or missing setup type"
                )

            setup_score = min(
                int(
                    base_stock.get(
                        "setup_score",
                        0
                    )
                ),
                int(
                    top_stock.get(
                        "setup_score",
                        0
                    )
                )
            )

            volume_ratio = float(
                top_stock.get(
                    "volume_ratio",
                    base_stock.get(
                        "volume_ratio",
                        0
                    )
                )
            )

            rsi = float(
                top_stock.get(
                    "rsi",
                    base_stock.get(
                        "rsi",
                        0
                    )
                )
            )

            stock_vs_sector = float(
                top_stock.get(
                    "stock_vs_sector",
                    0
                )
            )

            sector_vs_spy = float(
                top_stock.get(
                    "sector_vs_spy",
                    0
                )
            )

            stock_vs_spy = float(
                top_stock.get(
                    "stock_vs_spy",
                    base_stock.get(
                        "relative_strength",
                        0
                    )
                )
            )

            market_ok = bool(
                top_stock.get(
                    "market_bullish",
                    False
                )
            )

            sector_ok = bool(
                top_stock.get(
                    "sector_bullish",
                    False
                )
            )

            base_setup_valid = bool(
                base_stock.get(
                    "setup_valid",
                    False
                )
            )

            top_setup_valid = bool(
                top_stock.get(
                    "setup_valid",
                    False
                )
            )

            if not base_setup_valid:
                reasons.append(
                    "Base scanner setup is not valid"
                )

            if not top_setup_valid:
                reasons.append(
                    "Top-down scanner setup is not valid"
                )

            if setup_score < MIN_SETUP_SCORE:
                reasons.append(
                    f"Setup score below {MIN_SETUP_SCORE}"
                )

            if not market_ok:
                reasons.append(
                    "Market is not bullish"
                )

            if not sector_ok:
                reasons.append(
                    "Sector is not bullish"
                )

            if setup_type == "EMA20_RECLAIM":
                if volume_ratio < RECLAIM_MIN_VOLUME_RATIO:
                    reasons.append(
                        f"Reclaim volume below "
                        f"{RECLAIM_MIN_VOLUME_RATIO}x average"
                    )

                if not (
                    RECLAIM_MIN_RSI
                    <= rsi
                    <= RECLAIM_MAX_RSI
                ):
                    reasons.append(
                        f"Reclaim RSI not in "
                        f"{RECLAIM_MIN_RSI}-{RECLAIM_MAX_RSI}"
                    )

            elif setup_type == "MOMENTUM_BREAKOUT":
                if volume_ratio < BREAKOUT_MIN_VOLUME_RATIO:
                    reasons.append(
                        f"Breakout volume below "
                        f"{BREAKOUT_MIN_VOLUME_RATIO}x average"
                    )

                if not (
                    BREAKOUT_MIN_RSI
                    <= rsi
                    <= BREAKOUT_MAX_RSI
                ):
                    reasons.append(
                        f"Breakout RSI not in "
                        f"{BREAKOUT_MIN_RSI}-{BREAKOUT_MAX_RSI}"
                    )

            if (
                REQUIRE_STOCK_BEATS_SECTOR
                and
                stock_vs_sector <= 0
            ):
                reasons.append(
                    "Stock is not outperforming sector"
                )

            if (
                REQUIRE_SECTOR_BEATS_SPY
                and
                sector_vs_spy <= 0
            ):
                reasons.append(
                    "Sector is not outperforming SPY"
                )

            if (
                REQUIRE_STOCK_BEATS_SPY
                and
                stock_vs_spy <= 0
            ):
                reasons.append(
                    "Stock is not outperforming SPY"
                )

            candidate = {
                "ticker": ticker,

                "sector": top_stock.get(
                    "sector",
                    base_stock.get(
                        "sector",
                        "Unknown"
                    )
                ),

                "sector_etf": top_stock.get(
                    "sector_etf",
                    "Unknown"
                ),

                "setup_type": setup_type,

                "setup_priority": (
                    1
                    if setup_type == "EMA20_RECLAIM"
                    else 0
                ),

                "grade": "A+",

                "setup_score": int(
                    setup_score
                ),

                "price": float(
                    top_stock.get(
                        "price",
                        base_stock.get(
                            "price",
                            0
                        )
                    )
                ),

                "prior_high20": float(
                    top_stock.get(
                        "prior_high20",
                        base_stock.get(
                            "prior_high20",
                            0
                        )
                    )
                ),

                "atr": float(
                    top_stock.get(
                        "atr",
                        base_stock.get(
                            "atr",
                            0
                        )
                    )
                ),

                "rsi": round(
                    rsi,
                    2
                ),

                "volume_ratio": round(
                    volume_ratio,
                    2
                ),

                "stock_vs_sector": round(
                    stock_vs_sector,
                    2
                ),

                "sector_vs_spy": round(
                    sector_vs_spy,
                    2
                ),

                "stock_vs_spy": round(
                    stock_vs_spy,
                    2
                ),

                "base_scanner": "PASS",
                "topdown_scanner": "PASS",
                "market": "PASS",
                "sector_trend": "PASS",
            }

            if reasons:
                rejected.append({
                    **candidate,
                    "reasons": reasons,
                })
            else:
                approved.append(
                    candidate
                )

    # Same order used in the validated strict-dual backtest.
    approved.sort(
        key=lambda stock: (
            stock["setup_score"],
            stock["setup_priority"],
            stock["sector_vs_spy"],
            stock["stock_vs_sector"],
            stock["stock_vs_spy"],
            stock["volume_ratio"],
        ),
        reverse=True
    )

    for rank, stock in enumerate(
        approved,
        start=1
    ):
        stock["current_rank"] = rank

        stock["risk_preview"] = (
            build_risk_preview(
                price=stock["price"],
                atr=stock["atr"]
            )
        )

    best_current = (
        approved[0]
        if approved
        else None
    )

    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    candidates_payload = {
        "generated_at": generated_at,

        "strategy":
            "STRICT_DUAL_CURRENT_BEST_OPPORTUNITY",

        "market_bullish":
            market_bullish,

        "rules": {
            "both_scanners_must_agree":
                True,

            "minimum_setup_score":
                MIN_SETUP_SCORE,

            "ema20_reclaim": {
                "minimum_volume_ratio":
                    RECLAIM_MIN_VOLUME_RATIO,

                "rsi_range": [
                    RECLAIM_MIN_RSI,
                    RECLAIM_MAX_RSI
                ]
            },

            "momentum_breakout": {
                "minimum_volume_ratio":
                    BREAKOUT_MIN_VOLUME_RATIO,

                "rsi_range": [
                    BREAKOUT_MIN_RSI,
                    BREAKOUT_MAX_RSI
                ]
            },

            "stock_must_beat_sector":
                REQUIRE_STOCK_BEATS_SECTOR,

            "sector_must_beat_spy":
                REQUIRE_SECTOR_BEATS_SPY,

            "stock_must_beat_spy":
                REQUIRE_STOCK_BEATS_SPY,

            "weekly_lock":
                False
        },

        "best_current":
            best_current,

        "approved":
            approved,

        "rejected":
            rejected,
    }

    with MASTER_CANDIDATES_JSON.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            candidates_payload,
            file,
            indent=2
        )

    best_payload = {
        "generated_at":
            generated_at,

        "status": (
            "A+ CURRENT CANDIDATE"
            if best_current
            else
            "WAIT"
        ),

        "selected":
            best_current,

        "approved_count":
            len(
                approved
            ),
    }

    if not market_bullish:
        best_payload["reason"] = (
            "SPY market regime is not bullish."
        )

    elif not best_current:
        best_payload["reason"] = (
            "No stock currently passes all strict A+ master rules."
        )

    with BEST_CURRENT_CANDIDATE_JSON.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            best_payload,
            file,
            indent=2
        )

    print()
    print("================================")
    print("CURRENT MASTER DECISION")
    print("================================")

    if not market_bullish:
        print()
        print("STATUS: WAIT")

        print(
            "Reason: SPY market regime is not bullish."
        )

        print()
        print(
            "No stock is locked. "
            "The next scan will evaluate the market again."
        )

        return

    if not best_current:
        print()
        print("STATUS: WAIT")

        print(
            "No stock currently passes all strict A+ rules."
        )

        print()
        print(
            "The system will scan again on the next run."
        )

        return

    selected = best_current

    print()
    print(
        "#1 BEST CURRENT SWING OPPORTUNITY"
    )

    print()

    print(
        "Ticker:",
        selected["ticker"]
    )

    print(
        "Setup Type:",
        selected["setup_type"]
    )

    print(
        "Sector:",
        selected["sector"]
    )

    print(
        "Sector ETF:",
        selected["sector_etf"]
    )

    print(
        "Grade:",
        selected["grade"]
    )

    print(
        "Setup Score:",
        f"{selected['setup_score']}/100"
    )

    print()
    print("Base Scanner: PASS")
    print("Top-Down Scanner: PASS")
    print("SPY Market: PASS")
    print("Sector Trend: PASS")

    print()

    print(
        "RSI:",
        selected["rsi"]
    )

    print(
        "Volume:",
        f"{selected['volume_ratio']}x average"
    )

    if (
        selected[
            "setup_type"
        ] == "MOMENTUM_BREAKOUT"
    ):
        print(
            "Prior 20-Day High:",
            f"${selected['prior_high20']:.2f}"
        )

    print(
        "Stock vs Sector:",
        f"{selected['stock_vs_sector']:+.2f}%"
    )

    print(
        "Sector vs SPY:",
        f"{selected['sector_vs_spy']:+.2f}%"
    )

    print(
        "Stock vs SPY:",
        f"{selected['stock_vs_spy']:+.2f}%"
    )

    risk = selected.get(
        "risk_preview"
    )

    if risk:
        print()
        print("RISK PREVIEW")

        print(
            "Entry reference:",
            f"${risk['entry_reference']}"
        )

        print(
            "Stop:",
            f"${risk['stop']}"
        )

        print(
            "Target:",
            f"${risk['target']}"
        )

        print(
            "Shares:",
            risk["shares"]
        )

        print(
            "Position Value:",
            f"${risk['position_value']}"
        )

        print(
            "Planned Risk:",
            f"${risk['planned_risk']}"
        )

        print(
            "Reward/Risk:",
            f"{risk['reward_risk']}:1"
        )

    if len(approved) > 1:
        print()
        print(
            "OTHER CURRENT A+ CANDIDATES"
        )

        print()

        for stock in approved[
            1:TOP_ALTERNATES_TO_SHOW
        ]:

            print(
                f"#{stock['current_rank']} "
                f"{stock['ticker']} "
                f"| {stock['setup_type']} "
                f"| {stock['sector']} "
                f"| Setup {stock['setup_score']}/100 "
                f"| Vol {stock['volume_ratio']}x "
                f"| Stock vs SPY {stock['stock_vs_spy']:+.2f}%"
            )

    print()
    print("================================")
    print("STATUS: BEST CURRENT CANDIDATE")
    print("================================")

    print()

    print(
        "No weekly lock is used."
    )

    print(
        "The next scan can replace this stock "
        "if a stronger A+ opportunity appears."
    )

    print()
    print("Saved:")
    print(MASTER_CANDIDATES_JSON)
    print(BEST_CURRENT_CANDIDATE_JSON)

    print()
    print("IMPORTANT:")

    print(
        "95/100 is a setup-quality score, "
        "not a 95% win probability."
    )


if __name__ == "__main__":
    main()
