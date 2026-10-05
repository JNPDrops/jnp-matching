"""Synthetic durable handover: no financial API or production database."""
from contextlib import contextmanager
from types import SimpleNamespace
import os
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from operations import routing_role as r, worker_write_fence as f


@unittest.skipUnless(os.environ.get('JNP_TEST_POSTGRES_DSN'), 'isolated local PostgreSQL not configured')
class RoutingHandoverTests(unittest.TestCase):
    @contextmanager
    def database(self):
        dsn = os.environ['JNP_TEST_POSTGRES_DSN']
        settings = conninfo_to_dict(dsn)
        self.assertIn(settings.get('host'), {'127.0.0.1','localhost','::1'})
        self.assertTrue(settings.get('dbname','').startswith('jnp_test_'))
        self.assertNotIn('service', settings)
        self.assertNotIn('hostaddr', settings)
        schema = 'handover_' + uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                with psycopg.connect(make_conninfo(dsn, options='-csearch_path='+schema), autocommit=True) as conn:
                    r.c.initialize(conn)
                    r.initialize_assignment(conn,3977752)
                    yield conn
            finally:
                admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def claim(self, conn, owner):
        return r.c.claim_role(conn,3977752,'routing',owner,require_assigned=True)

    def test_defaults_to_web_duplicate_instance_blocked_and_pause_survives_restart(self):
        with self.database() as conn:
            with self.assertRaises(r.c.LeaseUnavailable): self.claim(conn,r.WORKER)
            epoch = self.claim(conn,r.LEGACY)
            with self.assertRaisesRegex(r.c.LeaseUnavailable,'already_owned'): self.claim(conn,r.LEGACY)
            self.assertTrue(r.request_handover(conn,3977752,None))
            r.c.release_role(conn,3977752,'routing',r.LEGACY,epoch)
            r.initialize_assignment(conn,3977752)
            self.assertFalse(r.request_handover(conn,3977752,None))
            for owner in r.OWNERS:
                with self.assertRaises(r.c.LeaseUnavailable): self.claim(conn,owner)

    def test_handover_waits_for_release_then_only_target_claims_and_rollback_is_symmetric(self):
        with self.database() as conn:
            for source,target in [(r.LEGACY,r.WORKER),(r.WORKER,r.LEGACY)]:
                epoch=self.claim(conn,source)
                self.assertTrue(r.request_handover(conn,3977752,target))
                self.assertFalse(r.request_handover(conn,3977752,target))
                with self.assertRaises(r.c.LeaseUnavailable): self.claim(conn,target)
                self.assertTrue(r.c.heartbeat(conn,3977752,'routing',source,epoch,'ready')['draining'])
                r.c.release_role(conn,3977752,'routing',source,epoch)
                with self.assertRaises(r.c.LeaseUnavailable): self.claim(conn,source)
            epoch=self.claim(conn,r.LEGACY)
            snapshot=r.c.status_snapshot(conn,3977752)['roles'][0]
            self.assertEqual(snapshot['execution_location'],'web')
            self.assertEqual(snapshot['desired_location'],'web')
            self.assertTrue(snapshot['lease_live'])

    def test_unresolved_write_survives_drain_and_release_until_same_operation_audit_settles(self):
        with self.database() as conn:
            epoch=self.claim(conn,r.LEGACY)
            lease=SimpleNamespace(division=3977752,role='routing',owner=r.LEGACY,lease_id=epoch)
            operation=f.Operation(str(uuid4()),lease)
            f.admit_write(conn,operation,'allocation','PUT')
            r.request_handover(conn,3977752,r.WORKER)
            with self.assertRaises(f.WriteFenced):
                f.admit_write(conn,f.Operation(str(uuid4()),lease),'allocation','PUT')
            r.c.release_role(conn,3977752,'routing',r.LEGACY,epoch)
            r.c.initialize(conn)
            with self.assertRaisesRegex(r.c.LeaseUnavailable,'write_requires_review'): self.claim(conn,r.WORKER)
            f.settle_write(conn,operation)
            self.claim(conn,r.WORKER)
            with self.assertRaises(f.WriteFenced):
                f.admit_write(conn,f.Operation(str(uuid4()),lease),'allocation','PUT')
            self.assertEqual(conn.execute('SELECT state FROM jnp_worker_writes').fetchone()[0],'settled')


if __name__=='__main__': unittest.main()
