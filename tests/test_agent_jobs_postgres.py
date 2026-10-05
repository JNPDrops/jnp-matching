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
    def setup_queue(self,conn,role="fibonatix"):
        jobs.initialize(conn);roles.initialize_assignment(conn,3977752,role)
        epoch=c.claim_role(conn,3977752,role,'legacy-'+role,require_assigned=True)
        return SimpleNamespace(division=3977752,role=role,owner='legacy-'+role,lease_id=epoch)
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

    def test_icepay_does_not_claim_another_psp_job(self):
        with self.database() as (_,conn):
            fibo=self.setup_queue(conn)
            icepay=self.setup_queue(conn,'icepay')
            other=jobs.submit(conn,3977752,'fibonatix','synthetic','preflight',{},'same-request')
            own=jobs.submit(conn,3977752,'icepay','synthetic','run',{},'same-request')
            self.assertEqual(jobs.claim_next(conn,icepay)['job_id'],own['job_id'])
            self.assertEqual(jobs.claim_next(conn,fibo)['job_id'],other['job_id'])

    def test_reports_complete_once_and_do_not_restart_on_new_lease(self):
        with self.database() as (_,conn):
            lease=self.setup_queue(conn,'reports')
            original=jobs.submit(conn,3977752,'reports','synthetic-report','run',{},'once')
            job=jobs.claim_next(conn,lease);jobs.finish(conn,job,lease,'completed')
            roles.request_handover(conn,3977752,'reports','worker-reports')
            c.release_role(conn,3977752,'reports','legacy-reports',lease.lease_id)
            lease.owner='worker-reports'
            lease.lease_id=c.claim_role(conn,3977752,'reports',lease.owner,require_assigned=True)
            self.assertIsNone(jobs.claim_next(conn,lease))
            self.assertEqual(jobs.submit(conn,3977752,'reports','synthetic-report','run',{},'once')['job_id'],original['job_id'])
