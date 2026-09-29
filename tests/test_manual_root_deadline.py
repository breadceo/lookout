import sqlite3
import unittest
from contextlib import contextmanager

from src import cli, dashboard, db, monitor, profiles, router, tick


class ManualRootDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.SCHEMA)

    def tearDown(self):
        self.c.close()

    def test_old_manual_triage_root_expires_without_archiving_review(self):
        now = 10 * 86400
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('root','root','owner/manual',1,'head','monitoring',0,0)"""
        )
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/manual',1,'head','triage',0,0)"""
        )
        root = self.c.execute("SELECT * FROM cards WHERE key='root'").fetchone()
        old_policy = profiles.policy_for_repo
        try:
            profiles.policy_for_repo = lambda _repo: {"auto_review": False}
            expired = monitor.expire_manual_review_root(self.c, root, now=now, days=7)
        finally:
            profiles.policy_for_repo = old_policy

        self.assertTrue(expired)
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE key='root'").fetchone()["status"],
            "archived",
        )
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE key='review'").fetchone()["status"],
            "triage",
        )

    def test_auto_review_root_does_not_expire(self):
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('root','root','owner/auto',1,'head','monitoring',0,0)"""
        )
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/auto',1,'head','triage',0,0)"""
        )
        root = self.c.execute("SELECT * FROM cards WHERE key='root'").fetchone()
        old_policy = profiles.policy_for_repo
        try:
            profiles.policy_for_repo = lambda _repo: {"auto_review": True}
            expired = monitor.expire_manual_review_root(
                self.c, root, now=10 * 86400, days=7
            )
        finally:
            profiles.policy_for_repo = old_policy

        self.assertFalse(expired)
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE key='root'").fetchone()["status"],
            "monitoring",
        )

    def test_started_manual_review_keeps_root_monitoring(self):
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('root','root','owner/manual',1,'head','monitoring',0,0)"""
        )
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/manual',1,'head','reviewing',0,0)"""
        )
        root = self.c.execute("SELECT * FROM cards WHERE key='root'").fetchone()
        old_policy = profiles.policy_for_repo
        try:
            profiles.policy_for_repo = lambda _repo: {"auto_review": False}
            expired = monitor.expire_manual_review_root(
                self.c, root, now=10 * 86400, days=7
            )
        finally:
            profiles.policy_for_repo = old_policy

        self.assertFalse(expired)
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE key='root'").fetchone()["status"],
            "monitoring",
        )

    def test_manual_start_reactivates_expired_root(self):
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('pr-auto-review:owner/manual#1','root','owner/manual',1,'old','archived',0,0)"""
        )
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/manual',1,'head','triage',0,0)"""
        )
        review = self.c.execute("SELECT * FROM cards WHERE key='review'").fetchone()

        changed = router.reactivate_root_monitoring(self.c, review)

        self.assertTrue(changed)
        root = self.c.execute(
            "SELECT status,head_sha FROM cards WHERE key='pr-auto-review:owner/manual#1'"
        ).fetchone()
        self.assertEqual(dict(root), {"status": "monitoring", "head_sha": "head"})

    def test_dashboard_start_reactivates_expired_root(self):
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('pr-auto-review:owner/manual#1','root','owner/manual',1,'head','archived',0,0)"""
        )
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at,engine)
               VALUES ('review','review','owner/manual',1,'head','triage',0,0,'codex')"""
        )
        review_id = self.c.execute("SELECT id FROM cards WHERE key='review'").fetchone()["id"]
        old_connect = dashboard.db.connect
        old_ready = dashboard.engines.is_ready
        old_kick = dashboard.kick_tick

        @contextmanager
        def connect():
            yield self.c

        try:
            dashboard.db.connect = connect
            dashboard.engines.is_ready = lambda _engine: True
            dashboard.kick_tick = lambda: None
            self.assertTrue(dashboard.do_action("start", review_id, "codex"))
        finally:
            dashboard.db.connect = old_connect
            dashboard.engines.is_ready = old_ready
            dashboard.kick_tick = old_kick

        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE key='review'").fetchone()["status"],
            "intake",
        )
        self.assertEqual(
            self.c.execute(
                "SELECT status FROM cards WHERE key='pr-auto-review:owner/manual#1'"
            ).fetchone()["status"],
            "monitoring",
        )

    def test_cli_start_reactivates_expired_root(self):
        self.c.execute("ALTER TABLE cards ADD COLUMN engine TEXT")
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('pr-auto-review:owner/manual#1','root','owner/manual',1,'head','archived',0,0)"""
        )
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at,engine)
               VALUES ('review','review','owner/manual',1,'head','triage',0,0,'codex')"""
        )
        review_id = self.c.execute("SELECT id FROM cards WHERE key='review'").fetchone()["id"]
        old_init = cli.db.init
        old_connect = cli.db.connect

        @contextmanager
        def connect():
            yield self.c

        try:
            cli.db.init = lambda: None
            cli.db.connect = connect
            cli.cmd_start(review_id, "codex")
        finally:
            cli.db.init = old_init
            cli.db.connect = old_connect

        self.assertEqual(
            self.c.execute(
                "SELECT status FROM cards WHERE key='pr-auto-review:owner/manual#1'"
            ).fetchone()["status"],
            "monitoring",
        )

    def test_tick_skips_github_check_after_manual_root_expires(self):
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('root','root','owner/manual',1,'head','monitoring',0,0)"""
        )
        self.c.execute(
            """INSERT INTO cards(key,kind,repo,pr_number,head_sha,status,created_at,updated_at)
               VALUES ('review','review','owner/manual',1,'head','triage',0,0)"""
        )
        old_connect = tick.db.connect
        old_now = tick.db.now
        old_policy = profiles.policy_for_repo
        old_process = monitor.process_root
        calls = []

        @contextmanager
        def connect():
            yield self.c

        try:
            tick.db.connect = connect
            tick.db.now = lambda: 10 * 86400
            profiles.policy_for_repo = lambda _repo: {"auto_review": False}
            monitor.process_root = lambda *_args: calls.append(True)
            tick._monitor_roots()
        finally:
            tick.db.connect = old_connect
            tick.db.now = old_now
            profiles.policy_for_repo = old_policy
            monitor.process_root = old_process

        self.assertEqual(calls, [])
        self.assertEqual(
            self.c.execute("SELECT status FROM cards WHERE key='root'").fetchone()["status"],
            "archived",
        )


if __name__ == "__main__":
    unittest.main()
