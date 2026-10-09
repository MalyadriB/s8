"""Gap logging runs on the stream's own thread for every message, so one message must cost one write, however many instruments were quiet."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import storage
import stream_monitor
import test_straddle as base

T0 = datetime.fromisoformat("2026-09-21T10:00:00+05:30")


def message(*keys):
    return {"type": "live_feed", "feeds": {key: {"fullFeed": {"marketFF": {"ltpc": {"ltp": 1.0}}}} for key in keys}}


class GapLogging(base.TempDatabase):
    def setUp(self):
        super().setUp()
        self.addCleanup(storage.close_jsonl_files)  # an open file would stop the temporary folder being removed

    def test_all_the_gaps_of_one_message_are_written_together(self):
        storage.initialize_database()
        monitor = stream_monitor.StreamMonitor(5.0)
        keys = [f"NSE_FO|{n}" for n in range(30)]
        monitor.record(message(*keys), T0)  # first sight of each: no gap yet
        real = storage.get_connection
        with mock.patch("storage.get_connection", side_effect=real) as connections:
            gaps = monitor.record(message(*keys), T0 + timedelta(seconds=20))
        self.assertEqual(len(gaps), 30)
        self.assertEqual(connections.call_count, 1, "one database connection for the whole message, not one per gap")
        with storage.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM stream_gaps").fetchone()[0], 30)
            self.assertEqual(json.loads(connection.execute("SELECT details FROM stream_gaps ORDER BY id LIMIT 1").fetchone()[0])["gap_seconds"], 20.0)
        storage.flush_jsonl()
        lines = (Path(os.environ["DATA_DIR"]) / "gaps-2026-09-21.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 30)
        self.assertEqual(monitor.status()["gap_count"], 30)

    def test_no_gap_means_nothing_is_written(self):
        monitor = stream_monitor.StreamMonitor(5.0)
        monitor.record(message("NSE_FO|1"), T0)
        with mock.patch("storage.get_connection") as connections:
            self.assertEqual(monitor.record(message("NSE_FO|1"), T0 + timedelta(seconds=2)), [])
        connections.assert_not_called()

    def test_the_single_gap_helper_still_works(self):
        gap = {"instrument_key": "NSE_FO|1", "gap_seconds": 9.0}
        path = storage.append_gap_event(gap, T0)
        storage.flush_jsonl()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8").strip()), gap)


class BackgroundAppender(base.TempDatabase):
    def setUp(self):
        super().setUp()
        self.addCleanup(storage.close_jsonl_files)

    def test_queued_lines_reach_the_file_in_order(self):
        path = Path(os.environ["DATA_DIR"]) / "stream-2026-09-21.jsonl"
        for n in range(200):
            storage._jsonl.write(path, f"line {n}" + chr(10))
        storage.flush_jsonl()
        self.assertEqual(path.read_text(encoding="utf-8").splitlines(), [f"line {n}" for n in range(200)])

    def test_a_new_day_closes_the_previous_days_file_of_the_same_kind(self):
        folder = Path(os.environ["DATA_DIR"])
        first, second, other = folder / "stream-2026-09-21.jsonl", folder / "stream-2026-09-22.jsonl", folder / "gaps-2026-09-22.jsonl"
        for path in (first, other, second):
            storage._jsonl.write(path, "x" + chr(10))
        storage.flush_jsonl()
        self.assertNotIn(first, storage._jsonl._handles)
        self.assertIn(second, storage._jsonl._handles)
        self.assertIn(other, storage._jsonl._handles, "a different kind of file is untouched")
        storage.close_jsonl_files()
        first.unlink()  # closed handles can be deleted on Windows


if __name__ == "__main__":
    import unittest

    unittest.main()
