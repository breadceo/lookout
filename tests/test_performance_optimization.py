import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from src import db, ghclient, monitor, tick


class PerformanceOptimizationTest(unittest.TestCase):
    def test_events_lookup_index_exists(self):
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(db.SCHEMA)
            indexes = {row[1] for row in c.execute("PRAGMA index_list(events)")}
            self.assertIn("idx_events_key_type_id", indexes)
        finally:
            c.close()

    def test_pr_states_batches_tracked_prs_in_one_graphql_call(self):
        calls = []
        old_run = ghclient._run

        def fake_run(args, check=True):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {
                        "r0": {
                            "p0": {"number": 1, "state": "OPEN", "headRefOid": "h1"},
                            "p1": {"number": 2, "state": "MERGED", "headRefOid": "h2"},
                        },
                        "r1": {
                            "p0": {"number": 9, "state": "CLOSED", "headRefOid": "h9"},
                        },
                    }
                }),
            )

        try:
            ghclient._run = fake_run
            rows = ghclient.pr_states([
                ("owner/repo", 1), ("owner/repo", 2), ("other/project", 9)
            ])
        finally:
            ghclient._run = old_run

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["api", "graphql"])
        self.assertEqual(rows[("owner/repo", 1)]["headRefOid"], "h1")
        self.assertEqual(rows[("owner/repo", 2)]["state"], "MERGED")
        self.assertEqual(rows[("other/project", 9)]["state"], "CLOSED")

    def test_monitor_roots_uses_one_batch_and_passes_fresh_info(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        for n in (1, 2):
            c.execute(
                """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,'monitoring',?,?)""",
                (f"root-{n}", "root", "owner/repo", n, f"h{n}", db.now(), db.now()),
            )

        old_connect = tick.db.connect
        old_states = tick.ghclient.pr_states
        old_expire = monitor.expire_manual_review_root
        old_process = monitor.process_root
        state_calls = []
        processed = []

        @contextmanager
        def connect():
            yield c

        try:
            tick.db.connect = connect
            tick.ghclient.pr_states = lambda refs: (
                state_calls.append(list(refs))
                or {("owner/repo", n): {"number": n, "state": "OPEN", "headRefOid": f"h{n}"}
                    for n in (1, 2)}
            )
            monitor.expire_manual_review_root = lambda *_args: False
            monitor.process_root = lambda _c, card, info=None: processed.append(
                (card["pr_number"], info["headRefOid"])
            )
            tick._monitor_roots()
        finally:
            tick.db.connect = old_connect
            tick.ghclient.pr_states = old_states
            monitor.expire_manual_review_root = old_expire
            monitor.process_root = old_process
            c.close()

        self.assertEqual(state_calls, [[("owner/repo", 1), ("owner/repo", 2)]])
        self.assertEqual(processed, [(1, "h1"), (2, "h2")])


if __name__ == "__main__":
    unittest.main()
