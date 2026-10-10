"""Durable restart/lease tests, restricted to the isolated local CI database."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from operations import routing_completion as routing, processing_schedule as schedule
from operations import processing_alerts as alerts

DSN=os.environ.get('JNP_TEST_POSTGRES_DSN')


@unittest.skipUnless(DSN, 'requires isolated CI PostgreSQL')
class ProcessingPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        info=conninfo_to_dict(DSN)
        if info.get('host') not in {'127.0.0.1','localhost'} or not info.get('dbname','').startswith('jnp_test'):
            raise RuntimeError('isolated_test_database_required')
        self.psycopg=psycopg
        self.schema='processing_test_'+uuid4().hex
        self.conn=psycopg.connect(DSN,autocommit=True)
        self.conn.execute('CREATE SCHEMA '+self.schema)
        self.conn.execute('SET search_path TO '+self.schema)

    def tearDown(self):
        self.conn.execute('DROP SCHEMA '+self.schema+' CASCADE')
        self.conn.close()

    def connection(self):
        c=self.psycopg.connect(DSN)
        c.execute('SET search_path TO '+self.schema)
        c.commit()
        return c

    def test_restart_preserves_original_slot_and_outcome(self):
        now=datetime(2026,10,10,14,tzinfo=schedule.ZONE)
        schedule.register_due(self.conn,date(2026,10,10),now)
        first=self.conn.execute('SELECT * FROM jnp_processing_batches ORDER BY planned_at').fetchall()
        self.conn.execute("UPDATE jnp_processing_batches SET state='verified' WHERE batch_key=%s",(first[0][0],))
        with self.connection() as after_restart:
            schedule.register_due(after_restart,date(2026,10,10),now)
        second=self.conn.execute('SELECT * FROM jnp_processing_batches ORDER BY planned_at').fetchall()
        self.assertEqual(len(first),3)
        self.assertEqual(len(first),len(second))
        self.assertEqual(first[0][:7],second[0][:7])
        self.assertEqual(second[0][7],'verified')

    def test_live_lease_and_selected_queue_are_required(self):
        c=self.conn
        c.execute('CREATE TABLE jnp_debtor_route_control(enabled boolean,pause_reason text,cursor_at timestamptz)')
        c.execute('CREATE TABLE jnp_worker_roles(division int,role text,lease_id uuid,active_owner text,draining boolean,lease_until timestamptz)')
        c.execute('CREATE TABLE jnp_debtor_route_queue(reference text,order_reference text,state text)')
        lease=uuid4()
        owner=SimpleNamespace(role='routing',division=3977752,lease_id=lease,owner='worker-routing')
        c.execute("INSERT INTO jnp_debtor_route_control VALUES(true,NULL,now()-interval '1 minute')")
        c.execute("INSERT INTO jnp_worker_roles VALUES(3977752,'routing',%s,'worker-routing',false,now()+interval '2 minutes')",(lease,))
        routing.record(c,owner,{'state':'watching'})
        routing.require_recent(c,references=['TD12345'])
        c.execute("INSERT INTO jnp_debtor_route_queue VALUES('TD12345','TD12345','uncertain')")
        with self.assertRaisesRegex(routing.RoutingNotReady,'unfinished_routing'):
            routing.require_recent(c,references=['TD12345'])
        c.execute("UPDATE jnp_worker_roles SET lease_until=now()-interval '1 second'")
        with self.assertRaisesRegex(routing.RoutingNotReady,'owner_or_cursor'):
            routing.record(c,owner,{'state':'watching'})
        self.assertEqual(c.execute('SELECT count(*) FROM jnp_routing_completions').fetchone()[0],1)

    def test_uncertain_mail_is_not_sent_again_after_restart(self):
        key=alerts.enqueue(self.conn,'jnp:3977752:batch:2026-10-10T0900','routing','routing_stale')
        self.assertEqual(key,alerts.enqueue(self.conn,'jnp:3977752:batch:2026-10-10T0900','routing','routing_stale'))
        calls=[]
        class Transport:
            def __init__(self,settings):pass
            def authenticate(self):pass
            def submit(self,message):
                calls.append('submit')
                raise TimeoutError('synthetic timeout')
        env={'JNP_GRAPH_TENANT_ID':str(uuid4()),'JNP_GRAPH_CLIENT_ID':str(uuid4()),
             'JNP_GRAPH_CLIENT_SECRET':'synthetic','JNP_ALERT_FROM':'sender@example.test',
             'JNP_ALERT_TO':'recipient@example.test'}
        app=SimpleNamespace(_db_connect=self.connection)
        self.assertEqual(alerts.deliver_graph_one(app,env,Transport),'uncertain')
        self.assertEqual(alerts.deliver_graph_one(app,env,Transport),'idle')
        self.assertEqual(calls,['submit'])
        self.assertEqual(self.conn.execute('SELECT state FROM jnp_processing_alerts WHERE alert_key=%s',(key,)).fetchone()[0],'uncertain')

    def coordinator_fixture(self):
        from operations import processing_orchestrator as coordinator, agent_jobs
        coordinator.initialize(self.conn)
        agent_jobs.initialize(self.conn)
        lease=uuid4()
        self.conn.execute('CREATE TABLE jnp_worker_roles(division int,role text,lease_id uuid,active_owner text,draining boolean,lease_until timestamptz)')
        self.conn.execute("INSERT INTO jnp_worker_roles VALUES(3977752,'reports',%s,'worker-reports',false,now()+interval '2 minutes')",(lease,))
        self.conn.execute("INSERT INTO jnp_processing_control(division,enabled,first_planned_date) VALUES(3977752,true,'2026-10-10')")
        owner=SimpleNamespace(role='reports',division=3977752,lease_id=lease,owner='worker-reports')
        app=SimpleNamespace(DIVISION=3977752)
        now=datetime(2026,10,10,1,tzinfo=schedule.ZONE)
        return coordinator,owner,app,now

    def test_coordinator_does_not_repeat_a_queued_job(self):
        c,owner,app,now=self.coordinator_fixture()
        with patch('operations.worker_write_fence.current_owner',return_value=owner):
            self.assertEqual(c.tick(self.conn,app,now),'queued')
            self.assertEqual(c.tick(self.conn,app,now),'worker_still_running')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jnp_agent_jobs').fetchone()[0],1)

    def test_job_completion_without_adapter_proof_is_not_success(self):
        c,owner,app,now=self.coordinator_fixture()
        with patch('operations.worker_write_fence.current_owner',return_value=owner):
            c.tick(self.conn,app,now)
            self.conn.execute("UPDATE jnp_agent_jobs SET state='completed'")
            self.assertEqual(c.tick(self.conn,app,now),'dependency_blocked')
            self.assertEqual(c.tick(self.conn,app,now),'queued')
        states=dict(self.conn.execute('SELECT stage,state FROM jnp_processing_stages').fetchall())
        self.assertEqual(states['fibonatix_source'],'blocked')
        self.assertEqual(states['fibonatix_import'],'blocked')
        self.assertEqual(states['icepay_source'],'queued')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jnp_processing_alerts').fetchone()[0],2)

    def test_adapter_proof_does_not_allow_overlap_before_worker_finishes(self):
        c,owner,app,now=self.coordinator_fixture()
        with patch('operations.worker_write_fence.current_owner',return_value=owner):
            c.tick(self.conn,app,now)
            key,job=self.conn.execute("SELECT batch_key,job_id FROM jnp_processing_stages WHERE stage='fibonatix_source'").fetchone()
            c.outcome(self.conn,key,'fibonatix_source','verified',{'source_verified':True},job)
            self.assertEqual(c.tick(self.conn,app,now),'worker_still_running')
            self.conn.execute("UPDATE jnp_agent_jobs SET state='completed'")
            self.assertEqual(c.tick(self.conn,app,now),'queued')
        with self.assertRaisesRegex(ValueError,'immutable'):
            c.outcome(self.conn,key,'fibonatix_source','blocked',{'reason':'changed'})

    def test_watchdog_detects_unstarted_run_without_coordinator(self):
        c,owner,app,now=self.coordinator_fixture()
        c.watchdog(self.conn,app,now+timedelta(hours=4))
        reasons={r[0] for r in self.conn.execute('SELECT reason FROM jnp_processing_alerts').fetchall()}
        self.assertEqual(reasons,{'scheduled_run_not_started','scheduled_run_overdue','coordinator_heartbeat_missing'})
