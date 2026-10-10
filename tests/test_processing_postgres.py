"""Durable restart/lease tests, restricted to the isolated local CI database."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

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
