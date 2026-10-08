import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from src import db, ghclient, monitor, tick


class PerformanceOptimizationTest(unittest.TestCase):
    def test_events_lookup_index_covers_key_type_and_ts(self):
        c = sqlite3.connect(":memory:")
        try:
            c.executescript(db.SCHEMA)
            indexes = {row[1] for row in c.execute("PRAGMA index_list(events)")}
            self.assertIn("idx_events_key_type_ts", indexes)
            columns = [row[2] for row in c.execute(
                "PRAGMA index_info(idx_events_key_type_ts)"
            )]
            self.assertEqual(columns, ["key", "type", "ts"])
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

    def test_pr_states_keeps_successful_batches_when_a_later_batch_fails(self):
        calls = []
        old_run = ghclient._run

        def fake_run(args, check=True):
            calls.append(args)
            if len(calls) == 2:
                raise ghclient.GhError("temporary batch failure")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {"r0": {"p0": {
                        "number": 1, "state": "OPEN", "headRefOid": "h1"
                    }}}
                }),
            )

        try:
            ghclient._run = fake_run
            with self.assertLogs(ghclient.LOG, level="WARNING"):
                rows = ghclient.pr_states(
                    [("owner/repo", 1), ("owner/repo", 2)], batch_size=1
                )
        finally:
            ghclient._run = old_run

        self.assertEqual(len(calls), 2)
        self.assertEqual(rows, {
            ("owner/repo", 1): {"number": 1, "state": "OPEN", "headRefOid": "h1"}
        })

    def test_pr_states_keeps_partial_data_when_graphql_exits_one(self):
        payload = {
            "data": {
                "r0": {
                    "p0": {
                        "number": 1,
                        "state": "OPEN",
                        "headRefOid": "h1",
                        "author": {"login": "octocat"},
                    },
                    "p1": None,
                }
            },
            "errors": [{"type": "NOT_FOUND", "message": "missing PR"}],
        }
        proc = SimpleNamespace(
            returncode=1,
            stdout=json.dumps(payload),
            stderr="gh: Could not resolve to a PullRequest",
        )

        with patch.object(ghclient, "_run", return_value=proc) as run, \
             self.assertLogs(ghclient.LOG, level="WARNING"):
            rows = ghclient.pr_states([("owner/repo", 1), ("owner/repo", 99999)])

        self.assertFalse(run.call_args.kwargs.get("check", True))
        self.assertEqual(rows[("owner/repo", 1)]["author"]["login"], "octocat")
        self.assertNotIn(("owner/repo", 99999), rows)

    def test_pr_states_raises_with_stderr_when_graphql_has_no_data(self):
        proc = SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"data": None, "errors": [{"message": "denied"}]}),
            stderr="gh: GraphQL denied",
        )

        with patch.object(ghclient, "_run", return_value=proc), \
             self.assertLogs(ghclient.LOG, level="WARNING"):
            with self.assertRaisesRegex(ghclient.GhError, "GraphQL denied"):
                ghclient.pr_states([("owner/repo", 1)])

    def test_monitor_github_state_batches_roots_and_triage_once(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        now = db.now()
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('root','root','owner/repo',1,'h1','monitoring',?,?)""", (now, now)
        )
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/repo',2,'h2','triage',?,?)""", (now, now)
        )
        old_connect = tick.db.connect
        old_states = getattr(ghclient, "pr_states", None)
        old_root = monitor.process_root
        old_triage = monitor.process_triage
        calls, processed = [], []

        @contextmanager
        def connect():
            yield c

        def fake_states(refs):
            calls.append(list(refs))
            return {("owner/repo", n): {
                "number": n, "state": "OPEN", "headRefOid": f"h{n}"
            } for n in (1, 2)}

        try:
            tick.db.connect = connect
            ghclient.pr_states = fake_states
            monitor.process_root = lambda _c, card, info=None: processed.append(
                ("root", info["number"])
            )
            monitor.process_triage = lambda _c, card, info=None: processed.append(
                ("triage", info["number"])
            )
            tick._monitor_github_state()
        finally:
            tick.db.connect = old_connect
            if old_states is None:
                del ghclient.pr_states
            else:
                ghclient.pr_states = old_states
            monitor.process_root = old_root
            monitor.process_triage = old_triage
            c.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {("owner/repo", 1), ("owner/repo", 2)})
        self.assertEqual(processed, [("root", 1), ("triage", 2)])

    def test_monitor_missing_batch_entry_uses_per_card_fallback(self):
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        now = db.now()
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/repo',2,'h2','triage',?,?)""", (now, now)
        )
        seen = []

        @contextmanager
        def connect():
            yield c

        def fallback(_c, card, info=None):
            seen.append((card["key"], info))

        try:
            with patch.object(tick.db, "connect", connect), \
                 patch.object(ghclient, "pr_states", return_value={}), \
                 patch.object(monitor, "process_triage", fallback), \
                 patch.object(tick, "MAX_CONCURRENT", 1):
                tick._monitor_github_state()
        finally:
            c.close()

        self.assertEqual(seen, [("review", None)])

    def test_monitor_batch_failure_falls_back_and_logs_keyed_trace(self):
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        now = db.now()
        c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/repo',2,'h2','triage',?,?)""", (now, now)
        )

        @contextmanager
        def connect():
            yield c

        def failed_fallback(_c, card, info=None):
            raise ghclient.GhError("single PR fallback failed")

        try:
            with patch.object(tick.db, "connect", connect), \
                 patch.object(ghclient, "pr_states", side_effect=ghclient.GhError("batch failed")), \
                 patch.object(monitor, "process_triage", failed_fallback), \
                 patch.object(tick, "MAX_CONCURRENT", 1), \
                 self.assertLogs(tick.LOG, level="ERROR"):
                tick._monitor_github_state()
            rows = c.execute(
                "SELECT key, detail FROM events WHERE type='stage_error' ORDER BY id"
            ).fetchall()
        finally:
            c.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "review")
        detail = json.loads(rows[0]["detail"])
        self.assertEqual(detail["stage"], "monitor_triage")
        self.assertIn("single PR fallback failed", detail["trace"])


if __name__ == "__main__":
    unittest.main()
