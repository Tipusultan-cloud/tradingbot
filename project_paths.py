from pathlib import Path

# ============================================================
# CENTRAL PROJECT PATHS
# ============================================================

BASE_DIR = Path(__file__).parent

DATA_DIR = BASE_DIR / "data"

RESULTS_DIR = BASE_DIR / "results"

BACKTEST_RESULTS_DIR = (
    RESULTS_DIR
    / "backtests"
)

DOCS_DIR = BASE_DIR / "docs"


# ============================================================
# CREATE FOLDERS AUTOMATICALLY
# ============================================================

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

RESULTS_DIR.mkdir(
    parents=True,
    exist_ok=True
)

BACKTEST_RESULTS_DIR.mkdir(
    parents=True,
    exist_ok=True
)

DOCS_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# ACTIVE DATA FILES
# ============================================================

TOP_200_JSON = (
    DATA_DIR
    / "top_200_universe.json"
)

TOP_200_CSV = (
    DATA_DIR
    / "top_200_universe.csv"
)

QUALIFIED_SETUPS_JSON = (
    DATA_DIR
    / "qualified_setups.json"
)

QUALIFIED_TOPDOWN_JSON = (
    DATA_DIR
    / "qualified_setups_topdown.json"
)

MASTER_CANDIDATES_JSON = (
    DATA_DIR
    / "master_candidates.json"
)

BEST_CURRENT_CANDIDATE_JSON = (
    DATA_DIR
    / "best_current_candidate.json"
)

MASTER_WEEKLY_SELECTION_JSON = (
    DATA_DIR
    / "master_weekly_selection.json"
)


# ============================================================
# BACKTEST RESULT FILES
# ============================================================

BACKTEST_TRADES_CSV = (
    BACKTEST_RESULTS_DIR
    / "backtest_trades.csv"
)

BACKTEST_BY_SECTOR_CSV = (
    BACKTEST_RESULTS_DIR
    / "backtest_by_sector.csv"
)

BACKTEST_BY_SCORE_CSV = (
    BACKTEST_RESULTS_DIR
    / "backtest_by_score.csv"
)

BACKTEST_SUMMARY_CSV = (
    BACKTEST_RESULTS_DIR
    / "backtest_summary.csv"
)

BENCHMARK_COMPARISON_CSV = (
    BACKTEST_RESULTS_DIR
    / "benchmark_comparison.csv"
)
