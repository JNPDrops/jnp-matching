"""Synthetic cross-process write fencing; never accesses Exact or production DB."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import multiprocessing
import os
from types import SimpleNamespace
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from operations import worker_coordination as c
from operations import worker_write_fence as f


def admission_process(dsn, lease_id, operation_id, results):
    lease = SimpleNamespace(division=3977752, role='routing', owner='old-owner', lease_id=lease_id)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            f.admit_write(conn, f.Operation(operation_id, lease), 'allocation', 'PUT')
        results.put('admitted')
    except f.WriteFenced:
        results.put('fenced')
    except Exception as exc:
        results.put(type(exc).__name__)


@unittest.skipUnless(os.environ.get('JNP_TEST_POSTGRES_DSN'), 'isolated local PostgreSQL not configured')
class WriteFenceProcessTests(unittest.TestCase):
    @contextmanager
    def database(self):
        dsn = os.environ['JNP_TEST_POSTGRES_DSN']
        settings = conninfo_to_dict(dsn)
        self.assertIn(settings.get('host'), {'127.0.0.1', 'localhost', '::1'})
        self.assertTrue(settings.get('dbname', '').startswith('jnp_test_'))
        self.assertNotIn('service', settings)
        self.assertNotIn('hostaddr', settings)
        schema = 'fence_' + uuid4().hex
        scoped = make_conninfo(dsn, options='-csearch_path=' + schema)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                with psycopg.connect(scoped, autocommit=True) as conn:
                    c.initialize(conn)
                    yield scoped, conn
            finally:
                admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def child_admit(self, dsn, lease_id, operation_id):
        ctx = multiprocessing.get_context('spawn')
        results = ctx.Queue()
        process = ctx.Process(target=admission_process, args=(dsn, lease_id, operation_id, results))
        process.start()
        try:
            process.join(15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            return results.get(timeout=2)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            results.close()

    def test_inflight_write_blocks_takeover_after_lease_expiry_then_fences_old_owner(self):
        with self.database() as (dsn, conn):
            lease_id = c.claim_role(conn, 3977752, 'routing', 'old-owner')
            operation_id = str(uuid4())
            self.assertEqual(self.child_admit(dsn, lease_id, operation_id), 'admitted')
            # Lease expiry must not be interpreted as proof of non-execution.
            conn.execute("UPDATE jnp_worker_roles SET lease_until=NOW()-INTERVAL '1 second'")
            with self.assertRaisesRegex(c.LeaseUnavailable, 'write_requires_review'):
                c.claim_role(conn, 3977752, 'routing', 'new-owner')
            snapshot = c.status_snapshot(conn, 3977752)
            self.assertEqual(snapshot['roles'][0]['unresolved_writes'], 1)
            self.assertFalse(snapshot['roles'][0]['lease_live'])
            lease = SimpleNamespace(division=3977752, role='routing', lease_id=lease_id)
            f.settle_write(conn, f.Operation(operation_id, lease))
            c.claim_role(conn, 3977752, 'routing', 'new-owner')
            self.assertEqual(self.child_admit(dsn, lease_id, str(uuid4())), 'fenced')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jnp_worker_writes').fetchone()[0], 1)

    def test_drain_release_and_restart_cannot_clear_unresolved_write(self):
        with self.database() as (dsn, conn):
            lease_id = c.claim_role(conn, 3977752, 'routing', 'old-owner')
            operation_id = str(uuid4())
            self.assertEqual(self.child_admit(dsn, lease_id, operation_id), 'admitted')
            c.begin_drain(conn, 3977752, 'routing', desired_owner='new-owner')
            c.release_role(conn, 3977752, 'routing', 'old-owner', lease_id)
            # Initialization is idempotent and must retain unresolved history.
            c.initialize(conn)
            with self.assertRaisesRegex(c.LeaseUnavailable, 'write_requires_review'):
                c.claim_role(conn, 3977752, 'routing', 'new-owner')
            with self.assertRaises(f.WriteFenced):
                wrong = SimpleNamespace(division=3977752, role='routing', lease_id=str(uuid4()))
                f.settle_write(conn, f.Operation(operation_id, wrong))
            lease = SimpleNamespace(division=3977752, role='routing', lease_id=lease_id)
            f.settle_write(conn, f.Operation(operation_id, lease))
            c.claim_role(conn, 3977752, 'routing', 'new-owner')
            self.assertEqual(c.status_snapshot(conn,3977752)['roles'][0]['unresolved_writes'], 0)

    def test_write_admission_and_expired_takeover_are_serialized(self):
        with self.database() as (dsn, conn):
            # A contender's time is advanced to simulate expiry at precisely
            # the admission boundary. Either admission wins or takeover does.
            lease_id = c.claim_role(conn,3977752,'routing','old-owner')
            ctx = multiprocessing.get_context('spawn')
            results = ctx.Queue()
            process = ctx.Process(target=admission_process, args=(dsn,lease_id,str(uuid4()),results))
            process.start()
            try:
                try:
                    c.claim_role(conn,3977752,'routing','new-owner',
                                 now=datetime.now(timezone.utc)+timedelta(seconds=100))
                    takeover = True
                except c.LeaseUnavailable:
                    takeover = False
                process.join(15)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode,0)
                admitted = results.get(timeout=2)
                self.assertIn(admitted, {'admitted','fenced'})
                self.assertNotEqual(takeover, admitted == 'admitted')
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                results.close()


if __name__ == '__main__':
    unittest.main()
