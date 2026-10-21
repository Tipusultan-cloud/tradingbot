import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app
from app import _read_dashboard_json


class DashboardJsonTests(unittest.TestCase):
    def test_reads_json_object(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text(
                json.dumps({"status": "ready"}),
                encoding="utf-8",
            )
            errors = []

            result = _read_dashboard_json(path, "Test results", errors)

        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(errors, [])

    def test_reports_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            errors = []

            result = _read_dashboard_json(path, "Test results", errors)

        self.assertEqual(result, {})
        self.assertEqual(errors[0]["source"], "Test results")
        self.assertIn("not available", errors[0]["message"])

    def test_reports_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text("{invalid", encoding="utf-8")
            errors = []

            result = _read_dashboard_json(path, "Test results", errors)

        self.assertEqual(result, {})
        self.assertEqual(errors[0]["source"], "Test results")
        self.assertIn("Could not read results", errors[0]["message"])

    def test_master_candidate_requires_best_current(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.json"
            path.write_text(
                json.dumps({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "best_current": {"ticker": "ABC", "setup_score": 97},
                }),
                encoding="utf-8",
            )

            with patch.object(app, "MASTER_CANDIDATES_JSON", path):
                candidate, message = app.get_master_candidate("ABC")

        self.assertEqual(candidate["setup_score"], 97)
        self.assertEqual(message, "MASTER PASS")

    def test_reports_stale_dashboard_timestamp(self):
        message = app._timestamp_error(
            "2000-01-01T00:00:00+00:00",
            "Master selection",
        )

        self.assertIn("stale", message)

    def test_dashboard_hides_candidate_from_stale_master_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current_time = datetime.now(timezone.utc).isoformat()
            payloads = {
                "universe": {"generated_at": current_time, "stocks": []},
                "technical": {"generated_at": current_time, "qualified": []},
                "topdown": {
                    "generated_at": current_time,
                    "qualified": [],
                    "market": {},
                },
                "master": {
                    "generated_at": "2000-01-01T00:00:00+00:00",
                    "best_current": {"ticker": "OLD"},
                    "approved": [],
                },
                "best": {
                    "generated_at": current_time,
                    "status": "WAIT",
                    "selected": {"ticker": "OLD"},
                },
            }
            paths = {
                key: root / f"{key}.json"
                for key in payloads
            }
            for key, path in paths.items():
                path.write_text(json.dumps(payloads[key]), encoding="utf-8")

            with (
                patch.object(app, "TOP_200_JSON", paths["universe"]),
                patch.object(app, "QUALIFIED_SETUPS_JSON", paths["technical"]),
                patch.object(app, "QUALIFIED_TOPDOWN_JSON", paths["topdown"]),
                patch.object(app, "MASTER_CANDIDATES_JSON", paths["master"]),
                patch.object(app, "BEST_CURRENT_CANDIDATE_JSON", paths["best"]),
                patch.object(
                    app,
                    "get_paper_trades",
                    return_value={
                        "open": [],
                        "recent": [],
                        "summary": {
                            "total_trades": 0,
                            "open_trades": 0,
                            "closed_trades": 0,
                            "realized_pnl": 0,
                            "wins": 0,
                            "losses": 0,
                            "open_risk_at_stop": 0,
                            "win_rate": None,
                        },
                    },
                ),
            ):
                dashboard = app.get_dashboard_data()

        self.assertIsNone(dashboard["candidate"])
        self.assertEqual(dashboard["scans"]["master"]["status"], "stale")
        self.assertEqual(dashboard["scans"]["candidate"]["status"], "current")
        self.assertEqual(dashboard["candidate_status"], "STALE DATA")
        self.assertEqual(dashboard["status"], "degraded")

    def test_dashboard_hides_stale_best_candidate_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current_time = datetime.now(timezone.utc).isoformat()
            paths = {
                name: root / f"{name}.json"
                for name in ("universe", "technical", "topdown", "master", "best")
            }
            payloads = {
                "universe": {"generated_at": current_time, "stocks": []},
                "technical": {"generated_at": current_time, "qualified": []},
                "topdown": {
                    "generated_at": current_time,
                    "qualified": [],
                    "market": {},
                },
                "master": {
                    "generated_at": current_time,
                    "best_current": {"ticker": "ABC"},
                    "approved": [],
                },
                "best": {
                    "generated_at": "2000-01-01T00:00:00+00:00",
                    "status": "BUY",
                    "selected": {"ticker": "ABC"},
                },
            }
            for name, path in paths.items():
                path.write_text(json.dumps(payloads[name]), encoding="utf-8")

            with (
                patch.object(app, "TOP_200_JSON", paths["universe"]),
                patch.object(app, "QUALIFIED_SETUPS_JSON", paths["technical"]),
                patch.object(app, "QUALIFIED_TOPDOWN_JSON", paths["topdown"]),
                patch.object(app, "MASTER_CANDIDATES_JSON", paths["master"]),
                patch.object(app, "BEST_CURRENT_CANDIDATE_JSON", paths["best"]),
                patch.object(
                    app,
                    "get_paper_trades",
                    return_value={
                        "open": [],
                        "recent": [],
                        "summary": {
                            "total_trades": 0,
                            "open_trades": 0,
                            "closed_trades": 0,
                            "realized_pnl": 0,
                            "wins": 0,
                            "losses": 0,
                            "open_risk_at_stop": 0,
                            "win_rate": None,
                        },
                    },
                ),
            ):
                dashboard = app.get_dashboard_data()

        self.assertIsNone(dashboard["candidate"])
        self.assertEqual(dashboard["scans"]["candidate"]["status"], "stale")
        self.assertEqual(dashboard["candidate_status"], "STALE DATA")
        self.assertEqual(dashboard["status"], "degraded")

    def test_master_candidate_rejects_non_selected_ticker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.json"
            path.write_text(
                json.dumps({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "best_current": {"ticker": "XYZ"},
                    "approved": [{"ticker": "ABC"}],
                }),
                encoding="utf-8",
            )

            with patch.object(app, "MASTER_CANDIDATES_JSON", path):
                candidate, message = app.get_master_candidate("ABC")

        self.assertIsNone(candidate)
        self.assertIn("not the current best", message)

    def test_master_candidate_rejects_stale_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.json"
            path.write_text(
                json.dumps({
                    "generated_at": "2000-01-01T00:00:00+00:00",
                    "approved": [{"ticker": "ABC"}],
                }),
                encoding="utf-8",
            )

            with patch.object(app, "MASTER_CANDIDATES_JSON", path):
                candidate, message = app.get_master_candidate("ABC")

        self.assertIsNone(candidate)
        self.assertIn("stale", message)


if __name__ == "__main__":
    unittest.main()
