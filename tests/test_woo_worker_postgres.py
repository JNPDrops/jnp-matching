"""Real isolated PostgreSQL with synthetic rules only, never the Exact service."""
import asyncio
from contextlib import contextmanager
import multiprocessing
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from operations import assigned_role as roles, worker_coordination as c, worker_write_fence as f
from operations import woo_iban_rules as woo
from operations.test_woo_iban_rules import body, rule, ACCOUNT


def contender(dsn, gate, queue):
    gate.wait(10)
    try:
        with psycopg.connect(dsn,autocommit=True) as conn:
            epoch=c.claim_role(conn,3977752,'woo-rules','legacy-woo-rules',require_assigned=True)
            queue.put(('claimed',epoch))
    except c.LeaseUnavailable: queue.put(('blocked',None))
    except Exception as error: queue.put((type(error).__name__,None))


@unittest.skipUnless(os.environ.get('JNP_TEST_POSTGRES_DSN'),'isolated local PostgreSQL not configured')
class WooDatabaseTests(unittest.TestCase):
    @contextmanager
    def database(self):
        dsn=os.environ['JNP_TEST_POSTGRES_DSN']
        settings=conninfo_to_dict(dsn)
        self.assertIn(settings.get('host'),{'127.0.0.1','localhost','::1'})
        self.assertTrue(settings.get('dbname','').startswith('jnp_test_'))
        self.assertNotIn('service',settings)
        self.assertNotIn('hostaddr',settings)
        schema='woo_'+uuid4().hex
        scoped=make_conninfo(dsn,options='-csearch_path='+schema)
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                with psycopg.connect(scoped,autocommit=True) as conn:
                    c.initialize(conn)
                    woo.initialize(conn)
                    roles.initialize_assignment(conn,3977752,'woo-rules')
                    yield scoped,conn
            finally:
                admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def test_two_processes_with_same_owner_have_one_lease_and_pause_preserves_queue(self):
        with self.database() as (dsn,conn):
            roles.initialize_assignment(conn,3977752,'routing')
            routing_epoch=c.claim_role(conn,3977752,'routing','legacy-routing',require_assigned=True)
            conn.execute("INSERT INTO jnp_woo_iban_events(event_id,order_id,body,digest,state,attempts) VALUES('fixture',1,'{}','fixture','uncertain',4)")
            ctx=multiprocessing.get_context('spawn')
            gate,queue=ctx.Event(),ctx.Queue()
            children=[ctx.Process(target=contender,args=(dsn,gate,queue)) for _ in range(2)]
            try:
                for process in children: process.start()
                gate.set()
                for process in children:
                    process.join(15)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode,0)
                rows=[queue.get(timeout=2) for _ in children]
                self.assertEqual(sorted(row[0] for row in rows),['blocked','claimed'])
                epoch=next(row[1] for row in rows if row[0]=='claimed')
                roles.request_handover(conn,3977752,'woo-rules',None)
                c.release_role(conn,3977752,'woo-rules','legacy-woo-rules',epoch)
                woo.initialize(conn)
                roles.initialize_assignment(conn,3977752,'woo-rules')
                with self.assertRaises(c.LeaseUnavailable):
                    c.claim_role(conn,3977752,'woo-rules','worker-woo-rules',require_assigned=True)
                self.assertEqual(conn.execute("SELECT state,attempts FROM jnp_woo_iban_events WHERE event_id='fixture'").fetchone(),('uncertain',4))
                routing=conn.execute("SELECT lease_id,draining FROM jnp_worker_roles WHERE role='routing'").fetchone()
                self.assertEqual(str(routing[0]),routing_epoch)
                self.assertFalse(routing[1])
            finally:
                for process in children:
                    if process.is_alive(): process.terminate(); process.join(5)
                queue.close()

    def test_success_settles_after_checkpoint_but_failed_readback_blocks_handover(self):
        with self.database() as (dsn,conn):
            epoch=c.claim_role(conn,3977752,'woo-rules','legacy-woo-rules',require_assigned=True)
            lease=SimpleNamespace(database_url=dsn,division=3977752,role='woo-rules',
                owner='legacy-woo-rules',lease_id=epoch,lost=asyncio.Event(),_stop=asyncio.Event())
            app=SimpleNamespace(DATABASE_URL=dsn,DIVISION=3977752)
            send=AsyncMock(return_value=SimpleNamespace(status_code=201,headers={}))
            async def create(*_): return await c.budgeted_http(app,'woo-rules','POST',send)
            for number,collection in [(1,[rule()]),(2,[])]:
                event='fixture-'+str(number)
                conn.execute("INSERT INTO jnp_woo_iban_events(event_id,order_id,body,digest) VALUES(%s,%s,'{}','fixture')",(event,number))
                api=SimpleNamespace(account=AsyncMock(return_value=ACCOUNT),
                    rules=AsyncMock(side_effect=[[],collection]),create=create)
                with f.owner_scope(lease): asyncio.run(woo.process(conn,api,event,body(),'pending'))
            self.assertEqual(conn.execute('SELECT state FROM jnp_woo_iban_events ORDER BY event_id').fetchall(),[('done',),('uncertain',)])
            self.assertEqual(conn.execute('SELECT state FROM jnp_woo_rule_attempts ORDER BY created_at').fetchall(),[('confirmed',),('uncertain',)])
            self.assertEqual(conn.execute('SELECT state FROM jnp_worker_writes ORDER BY admitted_at').fetchall(),[('settled',),('unresolved',)])
            self.assertEqual(send.await_count,2)
            self.assertTrue(lease.lost.is_set())
            roles.request_handover(conn,3977752,'woo-rules','worker-woo-rules')
            c.release_role(conn,3977752,'woo-rules','legacy-woo-rules',epoch)
            with self.assertRaisesRegex(c.LeaseUnavailable,'write_requires_review'):
                c.claim_role(conn,3977752,'woo-rules','worker-woo-rules',require_assigned=True)
            status=c.status_snapshot(conn,3977752)['roles'][0]
            self.assertEqual(status['desired_location'],'worker')
            self.assertEqual(status['unresolved_writes'],1)
