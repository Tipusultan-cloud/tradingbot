from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv

from datetime import datetime, timezone
import sqlite3

import os
import math
import json
import hmac
import time

from project_paths import (
    BEST_CURRENT_CANDIDATE_JSON,
    MASTER_CANDIDATES_JSON,
    PULLBACK_SETUPS_JSON,
    QUALIFIED_SETUPS_JSON,
    QUALIFIED_TOPDOWN_JSON,
    SLOT_SELECTION_JSON,
    TOP_200_JSON,
)
from paper_journal import (
    DuplicateSignalError,
    OpenTradeExistsError,
    PaperTradeExportError,
    count_open_trades,
    export_paper_trades_csv,
    get_paper_trades,
    record_entry,
    record_exit,
)
from shadow_exits import get_shadow_comparison
from slot_selector import SOURCE_A_PLUS, SOURCE_PULLBACK, max_open_trades
from broker import auto_trading_status, get_trading_client, latest_price
from paper_journal import get_pending_broker_orders
from trade_manager import SOURCE_MANUAL, TradeError, open_trade
from trade_manager import sync as broker_sync


# ==========================================
# LOAD ENVIRONMENT
# ==========================================

load_dotenv()

app = FastAPI()
dashboard_app = FastAPI(
    title="TradingBot Dashboard",
    docs_url=None,
    redoc_url=None,
)


# ==========================================
# SETTINGS
# ==========================================

WEBHOOK_SECRET = os.getenv(
    "TV_WEBHOOK_SECRET"
)

if not WEBHOOK_SECRET:

    raise RuntimeError(
        "TV_WEBHOOK_SECRET is missing from .env"
    )


ACCOUNT_BALANCE = float(
    os.getenv(
        "PAPER_ACCOUNT_BALANCE",
        "10000"
    )
)


RISK_PER_TRADE = (

    float(
        os.getenv(
            "RISK_PER_TRADE_PCT",
            "1"
        )
    )

    / 100
)


ATR_MULTIPLIER = float(
    os.getenv(
        "ATR_MULTIPLIER",
        "1.5"
    )
)


REWARD_RISK_RATIO = float(
    os.getenv(
        "REWARD_RISK_RATIO",
        "2"
    )
)


SCANNER_MAX_AGE_HOURS = float(
    os.getenv(
        "SCANNER_MAX_AGE_HOURS",
        "36"
    )
)


MAX_OPEN_TRADES = max_open_trades()


# Dashboard refreshes sync Alpaca fills and exits at most this often.
BROKER_SYNC_SECONDS = 60
_last_broker_sync = 0.0

# The Buy button only works from the local dashboard page.
DASHBOARD_ORIGINS = {
    "http://127.0.0.1:8001",
    "http://localhost:8001",
}


# ==========================================
# QUALIFIED SCANNER FILE
# ==========================================

QUALIFIED_FILE = QUALIFIED_SETUPS_JSON


# ==========================================
# DUPLICATE PROTECTION
# ==========================================

# ==========================================
# LOAD SCANNER RESULT
# ==========================================

def get_scanner_setup(ticker):
    data, error = _load_fresh_result(
        QUALIFIED_FILE,
        "Scanner",
    )
    if error:
        return None, error

    qualified = data.get("qualified")
    if not isinstance(qualified, list):
        return None, "Scanner results have no valid qualified list"

    for stock in qualified:
        if not isinstance(stock, dict):
            continue

        saved_ticker = str(stock.get("ticker", "")).upper()
        if saved_ticker == ticker:
            return stock, "SCANNER PASS"

    return None, "Ticker is not currently 4/4 qualified"


def get_master_candidate(ticker):
    data, error = _load_fresh_result(
        MASTER_CANDIDATES_JSON,
        "Master selection",
    )
    if error:
        return None, error

    # Any approved A+ stock can be picked by hand, not only the best one.
    candidates = [data.get("best_current")]
    approved = data.get("approved")
    if isinstance(approved, list):
        candidates += approved
    for candidate in candidates:
        if isinstance(candidate, dict):
            saved_ticker = str(candidate.get("ticker", "")).upper()
            if saved_ticker == ticker:
                return candidate, "MASTER PASS"

    return None, "Ticker is not an approved A+ master candidate"


def get_slot_candidate(ticker):
    data, error = _load_fresh_result(
        SLOT_SELECTION_JSON,
        "Slot selection",
    )
    if error:
        return None, error

    selections = data.get("selections")
    if not isinstance(selections, list):
        return None, "Slot selection has no valid selections list"

    # The bot's slot picks first, then any stock in the Bot 2 list.
    bots = data.get("bots")
    pullback_list = (
        bots.get(SOURCE_PULLBACK)
        if isinstance(bots, dict)
        else None
    )
    candidates = selections + (
        pullback_list if isinstance(pullback_list, list) else []
    )
    for stock in candidates:
        if (
            isinstance(stock, dict)
            and str(stock.get("ticker", "")).upper() == ticker
            and stock.get("source") == SOURCE_PULLBACK
        ):
            return stock, "PULLBACK SLOT PASS"

    return None, "Ticker is not in the current pullback list"


def _timestamp_error(generated_at, label):
    if not isinstance(generated_at, str):
        return f"{label} results have no valid generated_at timestamp"

    try:
        scan_time = datetime.fromisoformat(generated_at)
    except ValueError:
        return f"{label} results have an invalid generated_at timestamp"

    if scan_time.tzinfo is None:
        scan_time = scan_time.replace(tzinfo=timezone.utc)

    age_hours = (
        datetime.now(timezone.utc) - scan_time
    ).total_seconds() / 3600

    if age_hours < -0.05:
        return f"{label} results are dated in the future"
    if age_hours > SCANNER_MAX_AGE_HOURS:
        return f"{label} results are stale: {age_hours:.1f} hours old"

    return None


def _load_fresh_result(path, label):
    if not path.exists():
        return None, f"{label} results do not exist"

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{label} results could not be read: {exc}"

    if not isinstance(data, dict):
        return None, f"{label} results must contain a JSON object"

    timestamp_error = _timestamp_error(data.get("generated_at"), label)
    if timestamp_error:
        return None, timestamp_error

    return data, None


# ==========================================
# HOME
# ==========================================

@app.get("/")
def home():

    return {

        "status":
            "running",

        "message":
            "Trading server is working"
    }


def _read_dashboard_json(path, source, errors):
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except FileNotFoundError:
        errors.append({
            "source": source,
            "message": "Results are not available yet. Run the daily scan.",
        })
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        errors.append({
            "source": source,
            "message": f"Could not read results: {exc}",
        })
        return {}

    if not isinstance(payload, dict):
        errors.append({
            "source": source,
            "message": "Results file must contain a JSON object.",
        })
        return {}

    return payload


def get_dashboard_data():
    errors = []
    universe = _read_dashboard_json(
        TOP_200_JSON, "Universe", errors
    )
    qualified = _read_dashboard_json(
        QUALIFIED_SETUPS_JSON, "Technical scan", errors
    )
    topdown = _read_dashboard_json(
        QUALIFIED_TOPDOWN_JSON, "Top-down scan", errors
    )
    master = _read_dashboard_json(
        MASTER_CANDIDATES_JSON, "Master selection", errors
    )
    best = _read_dashboard_json(
        BEST_CURRENT_CANDIDATE_JSON, "Current candidate", errors
    )
    pullback = _read_dashboard_json(
        PULLBACK_SETUPS_JSON, "Pullback scan", errors
    )
    slots = _read_dashboard_json(
        SLOT_SELECTION_JSON, "Slot selection", errors
    )

    scan_payloads = {
        "universe": ("Universe", universe),
        "technical": ("Technical scan", qualified),
        "topdown": ("Top-down scan", topdown),
        "master": ("Master selection", master),
        "pullback": ("Pullback scan", pullback),
        "slots": ("Slot selection", slots),
    }
    scan_statuses = {}
    for key, (source, payload) in scan_payloads.items():
        generated_at = payload.get("generated_at")
        if not payload:
            scan_statuses[key] = {
                "status": "unavailable",
                "generated_at": None,
                "age_hours": None,
            }
            continue

        freshness_error = _timestamp_error(generated_at, source)
        if freshness_error:
            errors.append({
                "source": source,
                "message": freshness_error,
            })

        age_hours = None
        if isinstance(generated_at, str):
            try:
                scan_time = datetime.fromisoformat(generated_at)
                if scan_time.tzinfo is None:
                    scan_time = scan_time.replace(tzinfo=timezone.utc)
                age_hours = max(
                    0,
                    (datetime.now(timezone.utc) - scan_time).total_seconds()
                    / 3600,
                )
            except ValueError:
                pass
        scan_statuses[key] = {
            "status": "stale" if freshness_error else "current",
            "generated_at": generated_at,
            "age_hours": age_hours,
        }

    candidate_freshness_error = (
        _timestamp_error(best.get("generated_at"), "Current candidate")
        if best
        else None
    )
    if candidate_freshness_error:
        errors.append({
            "source": "Current candidate",
            "message": candidate_freshness_error,
        })
    candidate_time = best.get("generated_at")
    candidate_age_hours = None
    if isinstance(candidate_time, str):
        try:
            parsed_candidate_time = datetime.fromisoformat(candidate_time)
            if parsed_candidate_time.tzinfo is None:
                parsed_candidate_time = parsed_candidate_time.replace(
                    tzinfo=timezone.utc
                )
            candidate_age_hours = max(
                0,
                (
                    datetime.now(timezone.utc) - parsed_candidate_time
                ).total_seconds() / 3600,
            )
        except ValueError:
            pass
    scan_statuses["candidate"] = {
        "status": (
            "unavailable"
            if not best
            else "stale" if candidate_freshness_error else "current"
        ),
        "generated_at": candidate_time,
        "age_hours": candidate_age_hours,
    }

    slot_selections = slots.get("selections", [])
    if not isinstance(slot_selections, list):
        slot_selections = []
        errors.append({
            "source": "Slot selection",
            "message": "Selections must be a list.",
        })
    slot_summary = {
        "status": (
            slots.get("status")
            if scan_statuses["slots"]["status"] == "current"
            else "STALE DATA"
        ),
        "max_slots": slots.get("max_slots", MAX_OPEN_TRADES),
        "free_slots": slots.get("free_slots"),
        "open_positions": slots.get("open_positions", []),
        # Stale picks are hidden so they are never mistaken for current ones.
        "selections": (
            slot_selections
            if scan_statuses["slots"]["status"] == "current"
            else []
        ),
        "warnings": slots.get("warnings", []),
        "bots": (
            slots.get("bots") or {}
            if scan_statuses["slots"]["status"] == "current"
            else {}
        ),
    }
    pullback_setups = pullback.get("qualified", [])
    if not isinstance(pullback_setups, list):
        pullback_setups = []

    qualified_setups = qualified.get("qualified", [])
    topdown_setups = topdown.get("qualified", [])
    approved = master.get("approved", [])
    universe_stocks = universe.get("stocks", [])

    if not isinstance(qualified_setups, list):
        qualified_setups = []
        errors.append({
            "source": "Technical scan",
            "message": "Qualified setups must be a list.",
        })
    if not isinstance(topdown_setups, list):
        topdown_setups = []
        errors.append({
            "source": "Top-down scan",
            "message": "Qualified setups must be a list.",
        })
    if not isinstance(approved, list):
        approved = []
        errors.append({
            "source": "Master selection",
            "message": "Approved candidates must be a list.",
        })
    if not isinstance(universe_stocks, list):
        universe_stocks = []
        errors.append({
            "source": "Universe",
            "message": "Stocks must be a list.",
        })

    master_candidate = None
    if (
        scan_statuses["master"]["status"] == "current"
        and scan_statuses["candidate"]["status"] == "current"
    ):
        if "best_current" in master:
            master_candidate = master.get("best_current")
        else:
            master_candidate = best.get("selected")

    topdown_market = topdown.get("market", {})
    if not isinstance(topdown_market, dict):
        topdown_market = {}
        errors.append({
            "source": "Top-down scan",
            "message": "Market summary must be an object.",
        })

    sector_ranking = universe.get("sector_ranking", [])
    if not isinstance(sector_ranking, list):
        sector_ranking = []
        errors.append({
            "source": "Universe",
            "message": "Sector ranking must be a list.",
        })

    broker = _broker_state(errors)

    try:
        paper_trades = get_paper_trades(limit=10)
    except (OSError, sqlite3.Error) as exc:
        paper_trades = {"open": [], "recent": []}
        errors.append({
            "source": "Paper journal",
            "message": f"Could not load paper journal or CSV export: {exc}",
        })

    try:
        shadow_exits = get_shadow_comparison()
    except (OSError, sqlite3.Error) as exc:
        shadow_exits = {"trades": [], "totals": {}}
        errors.append({
            "source": "Shadow exits",
            "message": f"Could not load shadow exit comparison: {exc}",
        })

    return {
        "status": "degraded" if errors else "ready",
        "generated_at": master.get("generated_at"),
        "market": topdown_market,
        "candidate": master_candidate,
        "candidate_status": (
            "STALE DATA"
            if (
                scan_statuses["master"]["status"] != "current"
                or scan_statuses["candidate"]["status"] != "current"
            )
            else best.get("status")
        ),
        "candidate_reason": best.get("reason") or master.get("reason"),
        "counts": {
            "universe": len(universe_stocks),
            "technical_setups": len(qualified_setups),
            "topdown_setups": len(topdown_setups),
            "approved": len(approved),
            "pullback_setups": pullback.get(
                "qualified_count", len(pullback_setups)
            ),
            "open_paper_trades": len(paper_trades["open"]),
            "realized_pnl": paper_trades["summary"]["realized_pnl"],
        },
        "sector_ranking": sector_ranking,
        "recent_setups": topdown_setups[:10],
        "slots": slot_summary,
        "broker": broker,
        "paper_trades": paper_trades,
        "shadow_exits": shadow_exits,
        "scans": scan_statuses,
        "errors": errors,
    }


def _broker_state(errors):
    """Auto-trading status, Alpaca orders waiting to fill, rate-limited sync."""
    global _last_broker_sync
    enabled, reason = auto_trading_status()
    if enabled and time.monotonic() - _last_broker_sync >= BROKER_SYNC_SECONDS:
        _last_broker_sync = time.monotonic()
        try:
            result = broker_sync(get_trading_client(), latest_price)
            for error in result["errors"]:
                errors.append({"source": "Alpaca", "message": error})
        except Exception as exc:  # network and API errors
            errors.append({
                "source": "Alpaca",
                "message": f"Could not sync with Alpaca: {exc}",
            })

    try:
        pending = get_pending_broker_orders()
    except (OSError, sqlite3.Error):
        pending = []
    return {"enabled": enabled, "reason": reason, "pending_orders": pending}


def find_listed_stock(ticker):
    """(stock, strategy) from the fresh Bot 1 / Bot 2 lists, or an error."""
    data, error = _load_fresh_result(SLOT_SELECTION_JSON, "Slot selection")
    if error:
        return None, None, error
    bots = data.get("bots")
    if not isinstance(bots, dict):
        return None, None, "Slot selection has no bot lists"
    for strategy in (SOURCE_A_PLUS, SOURCE_PULLBACK):
        for stock in bots.get(strategy) or []:
            if (
                isinstance(stock, dict)
                and str(stock.get("ticker", "")).upper() == ticker
            ):
                return stock, strategy, None
    return None, None, f"{ticker} is not in the current Bot 1 or Bot 2 list"


@dashboard_app.post("/api/buy")
async def dashboard_buy(request: Request):
    # A custom header forces a CORS preflight that this app never allows,
    # so other websites cannot make the browser send this request.
    if (
        request.headers.get("origin") not in DASHBOARD_ORIGINS
        or request.headers.get("x-tradingbot") != "dashboard"
    ):
        raise HTTPException(
            status_code=403,
            detail="Buy requests are only accepted from the local dashboard",
        )

    try:
        data = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON") from exc
    ticker = str((data or {}).get("ticker", "")).upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker is missing")

    enabled, reason = auto_trading_status()
    if not enabled:
        raise HTTPException(status_code=409, detail=reason)

    stock, strategy, error = find_listed_stock(ticker)
    if stock is None:
        raise HTTPException(status_code=409, detail=error)

    try:
        order = open_trade(
            get_trading_client(), stock, strategy, SOURCE_MANUAL, latest_price
        )
    except TradeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # Alpaca rejections and network errors
        raise HTTPException(
            status_code=502,
            detail=f"Alpaca did not accept the order: {exc}",
        ) from exc
    return {"status": "order_sent", **order}


@dashboard_app.get("/", response_class=FileResponse)
def dashboard():
    return FileResponse(
        os.path.join(
            os.path.dirname(__file__),
            "web",
            "dashboard.html",
        ),
        media_type="text/html",
    )


@dashboard_app.get("/api/health")
def dashboard_health():
    return {"status": "ok"}


@dashboard_app.get("/api/dashboard")
def dashboard_summary():
    return get_dashboard_data()


@dashboard_app.get("/api/paper-trades.csv")
def download_paper_trades_csv():
    try:
        export_path = export_paper_trades_csv()
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not prepare the paper-trade CSV export",
        ) from exc
    return FileResponse(
        export_path,
        media_type="text/csv",
        filename="paper_trades.csv",
    )


# ==========================================
# TRADINGVIEW WEBHOOK
# ==========================================

@app.post("/webhook")
async def webhook(request: Request):


    # ======================================
    # READ TRADINGVIEW JSON
    # ======================================

    try:
        data = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Request body must contain valid JSON",
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail="Request body must be a JSON object",
        )


    # ======================================
    # CHECK SECRET
    # ======================================

    supplied_secret = data.get("secret")
    if (
        not isinstance(supplied_secret, str)
        or not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET)
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid webhook secret"
        )


    # ======================================
    # READ BASIC DATA
    # ======================================

    ticker = str(
        data.get(
            "ticker",
            ""
        )
    ).upper().strip()


    action = str(
        data.get(
            "action",
            ""
        )
    ).upper().strip()


    try:

        price = float(
            data.get(
                "price"
            )
        )

    except (
        TypeError,
        ValueError
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid price"
        )


    if not ticker:

        raise HTTPException(
            status_code=400,
            detail="Ticker is missing"
        )


    if action not in [
        "BUY",
        "SELL"
    ]:

        raise HTTPException(
            status_code=400,
            detail="Action must be BUY or SELL"
        )


    if not math.isfinite(price) or price <= 0:

        raise HTTPException(
            status_code=400,
            detail="Price must be greater than 0"
        )


    # With auto trading on, Alpaca holds the positions; a journal-only
    # TradingView trade would not match the paper account.
    auto_enabled, _ = auto_trading_status()
    if auto_enabled:
        return {
            "status": "ignored",
            "ticker": ticker,
            "action": action,
            "reason": (
                "Auto trading is on: buys come from the bot and the "
                "dashboard Buy button, exits from the Alpaca stop and "
                "target orders."
            ),
        }


    # ======================================
    # BUY SIGNAL
    # ======================================

    if action == "BUY":


        # ==================================
        # GET ATR FROM TRADINGVIEW
        # ==================================

        try:

            atr = float(
                data.get(
                    "atr"
                )
            )

        except (
            TypeError,
            ValueError
        ):

            raise HTTPException(
                status_code=400,
                detail="Invalid ATR"
            )


        if not math.isfinite(atr) or atr <= 0:

            raise HTTPException(
                status_code=400,
                detail="ATR must be greater than 0"
            )


        # ==================================
        # CHECK PYTHON SCANNER
        # ==================================

        scanner_setup, scanner_message = (
            get_scanner_setup(
                ticker
            )
        )

        master_candidate, master_message = (
            get_master_candidate(ticker)
            if scanner_setup is not None
            else (None, None)
        )

        strategy = SOURCE_A_PLUS
        slot_setup, slot_message = None, None
        if scanner_setup is None or master_candidate is None:
            slot_setup, slot_message = get_slot_candidate(ticker)
            if slot_setup is not None:
                scanner_setup = slot_setup
                strategy = SOURCE_PULLBACK


        # ==================================
        # TRADINGVIEW PASSED
        # BUT SCANNER DID NOT
        # ==================================

        if scanner_setup is None:

            print()
            print(
                "================================"
            )

            print(
                "TRADINGVIEW SIGNAL RECEIVED"
            )

            print(
                "================================"
            )

            print(
                "Ticker:",
                ticker
            )

            print(
                "TradingView:",
                "PASS"
            )

            print(
                "Python Scanner:",
                "FAIL"
            )

            print(
                "Reason:",
                scanner_message
            )

            print()

            print(
                "FINAL STATUS: NOT CONFIRMED"
            )

            print(
                "================================"
            )

            print()


            return {

                "status":
                    "not_confirmed",

                "ticker":
                    ticker,

                "tradingview":
                    "PASS",

                "scanner":
                    "FAIL",

                "reason":
                    scanner_message,

                "pullback_slot":
                    slot_message
            }

        if strategy == SOURCE_A_PLUS and master_candidate is None:
            return {
                "status": "not_confirmed",
                "ticker": ticker,
                "tradingview": "PASS",
                "scanner": "PASS",
                "master": "FAIL",
                "reason": master_message,
                "pullback_slot": slot_message,
            }

        try:
            open_trades = count_open_trades()
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(
                status_code=500,
                detail="Could not read the paper trade journal",
            ) from exc

        if open_trades >= MAX_OPEN_TRADES:
            return {
                "status": "not_confirmed",
                "ticker": ticker,
                "tradingview": "PASS",
                "strategy": strategy,
                "reason": (
                    f"All {MAX_OPEN_TRADES} paper slots are full "
                    f"({open_trades} open trades)"
                ),
            }


        # ==================================
        # ALL APPROVAL SYSTEMS PASSED
        # ==================================

        max_risk_dollars = (

            ACCOUNT_BALANCE
            *
            RISK_PER_TRADE

        )


        stop_distance = (

            atr
            *
            ATR_MULTIPLIER

        )


        stop_price = (

            price
            -
            stop_distance

        )


        if stop_price <= 0:

            raise HTTPException(
                status_code=400,
                detail="Calculated stop price is invalid"
            )


        risk_per_share = (

            price
            -
            stop_price

        )


        shares_by_risk = math.floor(

            max_risk_dollars
            /
            risk_per_share

        )


        shares_by_cash = math.floor(

            ACCOUNT_BALANCE
            /
            price

        )


        shares = min(

            shares_by_risk,
            shares_by_cash

        )


        if shares < 1:

            raise HTTPException(
                status_code=400,
                detail="Position size below 1 share"
            )

        try:
            paper_trade = record_entry(
                ticker=ticker,
                entry_price=price,
                atr=atr,
                shares=shares,
                stop_price=stop_price,
                target_price=price + (
                    risk_per_share * REWARD_RISK_RATIO
                ),
                setup_score=scanner_setup.get("setup_score"),
                strategy=strategy,
            )
        except (DuplicateSignalError, OpenTradeExistsError) as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
            ) from exc
        except PaperTradeExportError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "The paper trade was saved in SQLite, but the CSV export "
                    "could not be updated."
                ),
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(
                status_code=500,
                detail="Could not persist the paper trade",
            ) from exc


        position_value = (

            shares
            *
            price

        )


        actual_risk = (

            shares
            *
            risk_per_share

        )


        target_price = (

            price

            +

            (
                risk_per_share
                *
                REWARD_RISK_RATIO
            )

        )


        # ==================================
        # PRINT FINAL CONFIRMATION
        # ==================================

        print()
        print(
            "================================"
        )

        print(
            "FINAL CONFIRMED SWING SETUP"
        )

        print(
            "================================"
        )

        print(
            "Ticker:",
            ticker
        )

        print(
            "Strategy:",
            strategy
        )

        print(
            "Sector:",
            scanner_setup.get(
                "sector"
            )
        )

        print()

        print(
            "Python Scanner: PASS"
        )

        print(
            "Basic Score:",
            f"{scanner_setup.get('basic_score')}/4"
        )

        print(
            "Setup Score:",
            f"{scanner_setup.get('setup_score')}/100"
        )

        print()

        print(
            "TradingView: PASS"
        )

        print()

        print(
            "Entry:",
            round(
                price,
                2
            )
        )

        print(
            "ATR:",
            round(
                atr,
                2
            )
        )

        print(
            "Stop:",
            round(
                stop_price,
                2
            )
        )

        print(
            "Target:",
            round(
                target_price,
                2
            )
        )

        print(
            "Shares:",
            shares
        )

        print(
            "Position Value:",
            round(
                position_value,
                2
            )
        )

        print(
            "Maximum Risk:",
            round(
                max_risk_dollars,
                2
            )
        )

        print(
            "Planned Risk:",
            round(
                actual_risk,
                2
            )
        )

        print(
            "Reward/Risk:",
            REWARD_RISK_RATIO
        )

        print()

        print(
            "FINAL STATUS: CONFIRMED"
        )

        print(
            "================================"
        )

        print()


        return {

            "status":
                "FINAL_CONFIRMED",

            "ticker":
                ticker,

            "sector":
                scanner_setup.get(
                    "sector"
                ),

            "scanner_score":
                scanner_setup.get(
                    "basic_score"
                ),

            "setup_score":
                scanner_setup.get(
                    "setup_score"
                ),

            "tradingview":
                "PASS",

            "strategy":
                strategy,

            "master": (
                "PASS"
                if strategy == SOURCE_A_PLUS
                else "NOT USED (pullback slot)"
            ),

            "entry_price":
                round(
                    price,
                    2
                ),

            "atr":
                round(
                    atr,
                    2
                ),

            "stop_price":
                round(
                    stop_price,
                    2
                ),

            "target_price":
                round(
                    target_price,
                    2
                ),

            "shares":
                shares,

            "position_value":
                round(
                    position_value,
                    2
                ),

            "planned_risk":
                round(
                    actual_risk,
                    2
                ),

            "reward_risk_ratio":
                REWARD_RISK_RATIO,

            "paper_trade_id":
                paper_trade["id"],

            "paper_trade_status":
                paper_trade["status"]
        }


    # ======================================
    # SELL / EXIT SIGNAL
    # ======================================

    try:
        closed_trade = record_exit(ticker, price)
    except DuplicateSignalError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except PaperTradeExportError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "The paper trade was updated in SQLite, but the CSV export "
                "could not be updated."
            ),
        ) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not update the paper trade journal",
        ) from exc

    if closed_trade is None:
        return {
            "status": "no_open_paper_trade",
            "ticker": ticker,
            "action": "SELL",
            "exit_price": round(price, 2),
        }


    print()
    print(
        "================================"
    )

    print(
        "SELL / EXIT SIGNAL RECEIVED"
    )

    print(
        "================================"
    )

    print(
        "Ticker:",
        ticker
    )

    print(
        "Exit Price:",
        round(
            price,
            2
        )
    )

    print(
        "================================"
    )

    print()


    return {

        "status":
            "approved",

        "ticker":
            ticker,

        "action":
            "SELL",

        "exit_price":
            round(
                price,
                2
            ),

        "paper_trade_id":
            closed_trade["id"],

        "realized_pnl":
            round(closed_trade["realized_pnl"], 2),

        "paper_trade_status":
            closed_trade["status"]
    }