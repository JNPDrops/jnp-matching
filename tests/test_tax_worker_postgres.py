"""Local isolated PostgreSQL only; every external response is synthetic."""
import asyncio
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
import multiprocessing
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from operations import assigned_role as roles, worker_coordination as c, worker_write_fence as f
from operations import tax_agent, tax_allocation as tax, tax_write_operations as writes
from operations import allocation_maintenance as maintenance
from operations.test_tax_allocation import PAYLOAD, PROPOSALS, WORDS, RULE, ACCOUNT


def contender(dsn, gate, queue):
    gate.wait(10)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            epoch=c.claim_role(conn,3977752,'tax','legacy-tax',require_assigned=True)
            queue.put(('claimed',epoch))
    except c.LeaseUnavailable: queue.put(('blocked',None))
    except Exception as error: queue.put((type(error).__name__,None))


@unittest.skipUnless(os.environ.get('JNP_TEST_POSTGRES_DSN'),'isolated local PostgreSQL not configured')
class TaxDatabaseTests(unittest.TestCase):
    @contextmanager
    def database(self):
        dsn=os.environ['JNP_TEST_POSTGRES_DSN'];settings=conninfo_to_dict(dsn)
        self.assertIn(settings.get('host'),{'localhost','127.0.0.1','::1'})
        self.assertTrue(settings.get('dbname','').startswith('jnp_test_'))
        self.assertNotIn('service',settings);self.assertNotIn('hostaddr',settings)
        schema='tax_'+uuid4().hex
        scoped=make_conninfo(dsn,options='-csearch_path='+schema)
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                with psycopg.connect(scoped,autocommit=True) as conn:
                    c.initialize(conn);tax_agent.initialize(conn);tax.initialize(conn)
                    maintenance.initialize_cleanup(conn)
                    roles.initialize_assignment(conn,3977752,'tax')
                    yield scoped,conn
            finally:
                admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def lease(self,dsn,conn):
        epoch=c.claim_role(conn,3977752,'tax','legacy-tax',require_assigned=True)
        return SimpleNamespace(database_url=dsn,division=3977752,role='tax',owner='legacy-tax',
            lease_id=epoch,lost=asyncio.Event(),_stop=asyncio.Event())

    def test_process_contention_and_pause_preserve_existing_tax_progress(self):
        with self.database() as (dsn,conn):
            roles.initialize_assignment(conn,3977752,'routing')
            routing=c.claim_role(conn,3977752,'routing','legacy-routing',require_assigned=True)
            cursor=datetime(2026,1,1,tzinfo=timezone.utc)
            conn.execute("UPDATE jnp_tax_control SET cursor_at=%s,enabled=false,scan_version='retained'",(cursor,))
            conn.execute("INSERT INTO jnp_tax_rules(words,payload,details,state) VALUES(%s,%s::jsonb,'{}','uncertain')",(WORDS,json.dumps(PAYLOAD)))
            ctx=multiprocessing.get_context('spawn');gate=ctx.Event();queue=ctx.Queue()
            children=[ctx.Process(target=contender,args=(dsn,gate,queue)) for _ in range(2)]
            try:
                for p in children:p.start()
                gate.set()
                for p in children:
                    p.join(15);self.assertFalse(p.is_alive());self.assertEqual(p.exitcode,0)
                results=[queue.get(timeout=2) for _ in children]
                self.assertEqual(sorted(r[0] for r in results),['blocked','claimed'])
                epoch=next(r[1] for r in results if r[0]=='claimed')
                roles.request_handover(conn,3977752,'tax',None)
                c.release_role(conn,3977752,'tax','legacy-tax',epoch)
                tax_agent.initialize(conn);tax.initialize(conn);roles.initialize_assignment(conn,3977752,'tax')
                self.assertEqual(conn.execute('SELECT cursor_at,enabled,scan_version FROM jnp_tax_control').fetchone(),(cursor,False,'retained'))
                self.assertEqual(conn.execute('SELECT state FROM jnp_tax_rules').fetchone(),('uncertain',))
                with self.assertRaises(c.LeaseUnavailable):
                    c.claim_role(conn,3977752,'tax','worker-tax',require_assigned=True)
                self.assertEqual(str(conn.execute("SELECT lease_id FROM jnp_worker_roles WHERE role='routing'").fetchone()[0]),routing)
            finally:
                for p in children:
                    if p.is_alive():p.terminate();p.join(5)
                queue.close()

    def test_audits_link_post_outcomes_and_uncertainty_blocks_transfer(self):
        with self.database() as (dsn,conn):
            lease=self.lease(dsn,conn);app=SimpleNamespace(DATABASE_URL=dsn,DIVISION=3977752)
            send=AsyncMock(return_value=SimpleNamespace(status_code=201,headers={}))
            async def request(method,*args,**kwargs):return await c.budgeted_http(app,'tax',method,send)
            for words,confirmed in [(WORDS,True),('second',False)]:
                payload={**PAYLOAD,'Words':words}
                api=SimpleNamespace(limits={},request=request,rules=AsyncMock(side_effect=[[],[{**payload,'ID':RULE}] if confirmed else []]))
                with f.owner_scope(lease):asyncio.run(tax.reconcile(conn,api,{words:{'payload':payload,'tax_bucket':'vpb'}}))
            self.assertEqual(conn.execute('SELECT state FROM jnp_tax_rule_attempts ORDER BY created_at').fetchall(),[('confirmed',),('uncertain',)])
            self.assertEqual(conn.execute('SELECT w.state FROM jnp_worker_writes w JOIN jnp_tax_rule_attempts t USING(operation_id) ORDER BY t.created_at').fetchall(),[('settled',),('unresolved',)])
            roles.request_handover(conn,3977752,'tax','worker-tax')
            c.release_role(conn,3977752,'tax','legacy-tax',lease.lease_id)
            with self.assertRaisesRegex(c.LeaseUnavailable,'write_requires_review'):
                c.claim_role(conn,3977752,'tax','worker-tax',require_assigned=True)
            snapshot=c.status_snapshot(conn,3977752)['roles'][0]
            self.assertEqual(snapshot['desired_location'],'worker')
            self.assertEqual(snapshot['unresolved_writes'],1)
            self.assertEqual(send.await_count,2)

    def test_cleanup_is_serialized_across_roles_and_never_retries_old_intent(self):
        with self.database() as (dsn,conn), psycopg.connect(dsn,autocommit=True) as other:
            rule=dict(ID='2a9fa4f3-56eb-462b-843a-919b4fed9e83',Account=ACCOUNT,AccountBankAccount='NL04RABO0200112244')
            key='jnp:allocation-rule:'+rule['ID']
            other.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))',(key,))
            api=SimpleNamespace(allowed_deletes=set(),rules=AsyncMock(return_value=[rule]),request=AsyncMock())
            self.assertEqual(asyncio.run(maintenance.delete_rule(conn,api,rule,'synthetic')),'busy_keep')
            api.rules.assert_not_awaited();api.request.assert_not_awaited()
            other.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(key,))
            lease=self.lease(dsn,conn);app=SimpleNamespace(DATABASE_URL=dsn,DIVISION=3977752)
            send=AsyncMock(side_effect=TimeoutError())
            async def request(method,*args,**kwargs):return await c.budgeted_http(app,'tax',method,send)
            api.request=request
            with f.owner_scope(lease),self.assertRaises(writes.ConfirmationRequired):
                asyncio.run(writes.retire_confirmed_rule(conn,api,rule,ACCOUNT))
            self.assertEqual(conn.execute('SELECT state FROM jnp_rule_cleanup_audit').fetchone(),('uncertain',))
            self.assertEqual(conn.execute('SELECT state FROM jnp_worker_writes').fetchone(),('unresolved',))
            self.assertEqual(asyncio.run(maintenance.delete_rule(other,api,rule,'other-role')),'uncertain')
            send.assert_awaited_once()

    def test_interrupted_scan_preserves_cursor_and_persists_read_status(self):
        with self.database() as (dsn,conn):
            cursor=datetime(2026,1,1,tzinfo=timezone.utc)
            conn.execute('UPDATE jnp_tax_control SET cursor_at=%s,scan_version=%s,metadata=%s::jsonb,metadata_at=NOW()',
                (cursor,tax_agent.SCAN_VERSION,json.dumps({'accounts':[],'tax_account_id':ACCOUNT})))
            app=SimpleNamespace(DATABASE_URL=dsn,DIVISION=3977752,BASE_URL=tax_agent.transport.BASE,
                _db_connect=lambda:nullcontext(conn))
            api=SimpleNamespace(rows=AsyncMock(side_effect=tax_agent.ScanDrained()))
            with patch.object(tax_agent,'TaxAPI',return_value=api),patch.object(tax_agent.allocation,'RoutingApp',return_value=app):
                asyncio.run(tax_agent.cycle(app))
            saved=conn.execute('SELECT cursor_at,summary FROM jnp_tax_control').fetchone()
            self.assertEqual(saved[0],cursor)
            self.assertEqual(saved[1]['state'],'draining')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jnp_tax_rules').fetchone()[0],0)
