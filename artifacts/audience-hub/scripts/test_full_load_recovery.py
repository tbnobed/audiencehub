"""Small verification-only tests; no database, PostgreSQL, or worker startup."""
from datetime import datetime, timedelta, timezone
import unittest

from full_load_recovery import phase_deltas


class RecoveryTimingTests(unittest.TestCase):
    def test_excludes_old_open_phase_and_downtime_using_observed_baseline(self):
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        before = {"jobs": [{
            "id": 9, "attempts": 1,
            "progress": {"phase": "attaching", "phase_elapsed_ms": {"resolving": 267000}},
        }]}
        baseline = {
            "id": 9, "attempts": 2,
            "progress": {"phase": "resolving",
                         "phase_elapsed_ms": {"resolving": 267000, "attaching": 3600000}},
        }
        final = {
            "id": 9, "attempts": 2, "type": "identity.resolve_batch",
            "started_at": started, "finished_at": started + timedelta(seconds=50),
            "progress": {"phase": None,
                         "phase_elapsed_ms": {"resolving": 287000, "attaching": 3630000}},
        }
        result = phase_deltas(before, {"jobs": [final]}, [{"jobs": [baseline]}])[0]
        self.assertEqual(result["raw_cumulative_phase_delta_seconds"]["attaching"], 3630)
        self.assertEqual(result["resumed_phase_seconds"], {"resolving": 20, "attaching": 30})
        self.assertEqual(result["attempt_wall_seconds"], 50)
        self.assertTrue(result["inherited_open_interval_warning"])

    def test_does_not_invent_phase_duration_when_baseline_was_not_observed(self):
        now = datetime.now(timezone.utc)
        before = {"jobs": [{"id": 9, "attempts": 1,
                            "progress": {"phase": "attaching", "phase_elapsed_ms": {}}}]}
        after = {"jobs": [{"id": 9, "attempts": 2, "type": "identity.resolve_batch",
                           "started_at": now, "finished_at": now,
                           "progress": {"phase": None, "phase_elapsed_ms": {"attaching": 100000}}}]}
        result = phase_deltas(before, after, [])[0]
        self.assertIsNone(result["resumed_phase_seconds"])
        self.assertTrue(result["inherited_open_interval_warning"])


if __name__ == "__main__":
    unittest.main()
