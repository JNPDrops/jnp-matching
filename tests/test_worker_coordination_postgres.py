"""Opt-in process tests against an isolated local PostgreSQL test database.

JNP_TEST_POSTGRES_DSN must explicitly name localhost and a jnp_test_* database.
No production DATABASE_URL, credentials, Exact calls or financial data are used.
"""
from datetime import datetime, timedelta, timezone
import multiprocessing
import os
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from operations.worker_coordination import (
    BudgetDeferred, LeaseUnavailable, claim_role, initialize, reserve_request, complete_request,
)


def role_process(dsn, gate, results, owner):
    try:
        gate.wait(10)
        with psycopg.connect(dsn, autocommit=True) as conn:
            results.put(("role", owner, "won", claim_role(conn, 3977752, "routing", owner)))
    except LeaseUnavailable:
        results.put(("role", owner, "blocked", None))
    except Exception as exc:
        results.put(("role", owner, type(exc).__name__, None))


def budget_process(dsn, gate, results, owner):
    try:
        gate.wait(10)
        with psycopg.connect(dsn, autocommit=True) as conn:
            reservation = reserve_request(conn, 3977752, "allocation", "routing", "GET", floor=0)
        results.put(("budget", owner, "won", reservation.request_id))
    except BudgetDeferred:
        results.put(("budget", owner, "blocked", None))
    except Exception as exc:
        results.put(("budget", owner, type(exc).__name__, None))


@unittest.skipUnless(os.environ.get("JNP_TEST_POSTGRES_DSN"), "isolated local PostgreSQL not configured")
class CoordinationProcessTests(unittest.TestCase):
    def test_late_response_and_older_pending_request_cannot_restore_budget(self):
        dsn = os.environ['JNP_TEST_POSTGRES_DSN']
        settings = conninfo_to_dict(dsn)
        self.assertIn(settings.get('host'), {'127.0.0.1', 'localhost', '::1'})
        self.assertTrue(settings.get('dbname', '').startswith('jnp_test_'))
        self.assertNotIn('service', settings)
        self.assertNotIn('hostaddr', settings)
        schema = 'quota_' + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                scoped = make_conninfo(dsn, options='-csearch_path=' + schema)
                with psycopg.connect(scoped, autocommit=True) as conn:
                    initialize(conn)
                    first = reserve_request(conn, 3977752, 'main', 'routing', 'GET', floor=0)
                    second = reserve_request(conn, 3977752, 'main', 'routing', 'GET', floor=0)
                    reset = str(int((datetime.now(timezone.utc) + timedelta(hours=4)).timestamp()*1000))
                    complete_request(conn, second, {'x-ratelimit-remaining': '1', 'x-ratelimit-reset': reset})
                    # The first call is still reserved despite predating the response.
                    with self.assertRaises(BudgetDeferred):
                        reserve_request(conn, 3977752, 'main', 'routing', 'GET', floor=0)
                    complete_request(conn, first, {'x-ratelimit-remaining': '2', 'x-ratelimit-reset': reset})
                    self.assertEqual(conn.execute("SELECT daily_remaining FROM jnp_exact_api_budget WHERE connection='main'").fetchone()[0], 1)
            finally:
                admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def test_competing_processes_get_one_role_and_one_last_budget_slot(self):
        dsn = os.environ["JNP_TEST_POSTGRES_DSN"]
        settings = conninfo_to_dict(dsn)
        self.assertIn(settings.get("host"), {"127.0.0.1", "localhost", "::1"})
        self.assertTrue(settings.get("dbname", "").startswith("jnp_test_"))
        self.assertNotIn("service", settings)
        self.assertNotIn("hostaddr", settings)
        schema = "coord_" + uuid.uuid4().hex
        scoped = make_conninfo(dsn, options="-csearch_path=" + schema)
        processes = []
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            try:
                with psycopg.connect(scoped, autocommit=True) as conn:
                    initialize(conn)
                    reset = int((datetime.now(timezone.utc) + timedelta(hours=4)).timestamp() * 1000)
                    minute_reset = int((datetime.now(timezone.utc) + timedelta(seconds=50)).timestamp() * 1000)
                    conn.execute("""INSERT INTO jnp_exact_api_budget
                        (division,connection,daily_limit,daily_remaining,daily_reset_ms,
                         minute_limit,minute_remaining,minute_reset_ms,observed_at)
                        VALUES(3977752,'allocation',5000,1,%s,60,10,%s,NOW())
                        ON CONFLICT(division,connection) DO UPDATE SET
                        daily_remaining=1,daily_reset_ms=EXCLUDED.daily_reset_ms,
                        minute_remaining=10,minute_reset_ms=EXCLUDED.minute_reset_ms,observed_at=NOW()""",
                        (reset, minute_reset))
                context = multiprocessing.get_context("spawn")
                gate, results = context.Event(), context.Queue()
                processes = [context.Process(target=role_process, args=(scoped, gate, results, f"worker-{n}")) for n in range(2)]
                processes += [context.Process(target=budget_process, args=(scoped, gate, results, f"caller-{n}")) for n in range(2)]
                for process in processes:
                    process.start()
                gate.set()
                for process in processes:
                    process.join(timeout=20)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
                rows = [results.get(timeout=2) for _ in processes]
                self.assertEqual(sorted(row[2] for row in rows if row[0] == "role"), ["blocked", "won"])
                self.assertEqual(sorted(row[2] for row in rows if row[0] == "budget"), ["blocked", "won"])
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    unittest.main()
