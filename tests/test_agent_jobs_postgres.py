import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

import test_tax_worker_postgres as fixtures
from operations import agent_jobs as jobs, assigned_role as roles, worker_coordination as c


@unittest.skipUnless(os.environ.get('JNP_TEST_POSTGRES_DSN'),'isolated local PostgreSQL not configured')
class DurableJobTests(unittest.TestCase):
    database=fixtures.TaxDatabaseTests.database
    def setup_queue(self,conn):
        jobs.initialize(conn);roles.initialize_assignment(conn,3977752,'fibonatix')
        epoch=c.claim_role(conn,3977752,'fibonatix','legacy-fibonatix',require_assigned=True)
        return SimpleNamespace(division=3977752,role='fibonatix',owner='legacy-fibonatix',lease_id=epoch)
    def test_submission_is_idempotent_and_one_lease_claims_once(self):
        with self.database() as (_,conn):
            lease=self.setup_queue(conn)
            job=jobs.submit(conn,3977752,'fibonatix','synthetic','preflight',{},'request')
            self.assertEqual(jobs.submit(conn,3977752,'fibonatix','synthetic','preflight',{},'request')['job_id'],job['job_id'])
            with self.assertRaises(jobs.JobConflict):jobs.submit(conn,3977752,'fibonatix','synthetic','import',{},'request')
            claimed=jobs.claim_next(conn,lease)
            self.assertEqual(claimed['job_id'],job['job_id'])
            self.assertIsNone(jobs.claim_next(conn,lease))
            jobs.finish(conn,claimed,lease,'completed')
            self.assertIsNone(jobs.claim_next(conn,lease))
            self.assertEqual(jobs.snapshot(conn,3977752,'fibonatix')[0]['state'],'completed')
    def test_lost_running_job_blocks_replay_after_role_transfer(self):
        with self.database() as (_,conn):
            lease=self.setup_queue(conn)
            jobs.submit(conn,3977752,'fibonatix','synthetic','import',{},'request')
            jobs.claim_next(conn,lease)
            roles.request_handover(conn,3977752,'fibonatix','worker-fibonatix')
            c.release_role(conn,3977752,'fibonatix','legacy-fibonatix',lease.lease_id)
            lease.owner='worker-fibonatix'
            lease.lease_id=c.claim_role(conn,3977752,'fibonatix',lease.owner,require_assigned=True)
            self.assertIsNone(jobs.claim_next(conn,lease))
            self.assertEqual(jobs.snapshot(conn,3977752,'fibonatix')[0]['state'],'uncertain')
            with self.assertRaises(jobs.JobConflict):jobs.submit(conn,3977752,'fibonatix','synthetic','import',{},'retry')
    def test_expiry_is_rechecked_after_waiting_and_drain_prevents_claim(self):
        with self.database() as (_,conn):
            lease=self.setup_queue(conn)
            job=jobs.submit(conn,3977752,'fibonatix','synthetic','preflight',{},'request',datetime.now(timezone.utc)+timedelta(hours=1))
            conn.execute("UPDATE jnp_agent_jobs SET expires_at=NOW()-INTERVAL '1 second'")
            self.assertIsNone(jobs.claim_next(conn,lease))
            self.assertEqual(jobs.snapshot(conn,3977752,'fibonatix')[0]['state'],'blocked')
            roles.request_handover(conn,3977752,'fibonatix',None)
            with self.assertRaises(c.LeaseUnavailable):jobs.claim_next(conn,lease)
