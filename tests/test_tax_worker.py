"""Synthetic rule collections only: never connects to Exact."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from operations import tax_allocation as tax, tax_agent, tax_write_operations as writes
from operations import worker_coordination as c, worker_write_fence as f, task_drain
from operations.test_tax_allocation import DB, WORDS, PAYLOAD, PROPOSALS, RULE, ACCOUNT


class Store(DB):
    def __init__(self, calls, fail_audit=False):
        super().__init__()
        self.calls = calls
        self.fail_audit = fail_audit
    def execute(self, sql, args=()):
        if 'jnp_tax_rule_attempts' in sql:
            self.calls.append(('audit', args[3], args[0]))
            if self.fail_audit and args[3] == 'confirmed':
                raise OSError('synthetic audit failure')
            return self
        if sql.startswith('UPDATE'):
            self.calls.append(('state', args[0]))
        return super().execute(sql, args)


class OwnedTaxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.db = Store(self.calls)
        self.lease = SimpleNamespace(database_url='synthetic', division=3977752, role='tax',
            owner='legacy-tax', lease_id=str(uuid4()), lost=asyncio.Event(), _stop=asyncio.Event())
        self.app = SimpleNamespace(DATABASE_URL='synthetic', DIVISION=3977752)
        self.send = AsyncMock(return_value=SimpleNamespace(status_code=201, headers={}))
        self.api = SimpleNamespace(limits={}, rules=AsyncMock(side_effect=[[], [{**PAYLOAD,'ID':RULE}]]),
                                   request=AsyncMock(side_effect=self.write))

    def database(self, url, function, *args, **kwargs):
        self.calls.append(('coordination', function.__name__))
        if function is c.reserve_request:
            return c.Reservation(str(uuid4()), 3977752, 'main', 'tax', 'POST')

    async def write(self, method, *args, **kwargs):
        return await c.budgeted_http(self.app, 'tax', method, self.send)

    async def reconcile(self, proposals=PROPOSALS):
        with patch.object(c, '_budget_database_call', side_effect=self.database), f.owner_scope(self.lease):
            return await tax.reconcile(self.db, self.api, proposals)

    async def test_post_settles_only_after_readback_state_and_correlated_audit(self):
        result = await self.reconcile()
        self.assertEqual(result['counts'], {'confirmed':1})
        self.assertEqual(self.calls[-1], ('coordination','settle_write'))
        audits = [r for r in self.calls if r[0]=='audit']
        self.assertEqual([r[1] for r in audits], ['intent','confirmed'])
        self.assertEqual(audits[0][2],audits[1][2])
        self.send.assert_awaited_once()

    async def test_two_rules_have_distinct_operations_and_settle_between_posts(self):
        other = {**PAYLOAD, 'Words':'synthetic-second-reference'}
        first = {**PAYLOAD,'ID':RULE}
        self.api.rules = AsyncMock(side_effect=[[], [first], [first,{**other,'ID':str(uuid4())}]])
        result = await self.reconcile({**PROPOSALS,other['Words']:{'payload':other,'tax_bucket':'btw'}})
        self.assertEqual(result['counts'], {'confirmed':2})
        intents = [r[2] for r in self.calls if r[:2]==('audit','intent')]
        self.assertEqual(len(set(intents)),2)
        actions = [r[1] for r in self.calls if r[0]=='coordination' and r[1] in ('admit_write','settle_write')]
        self.assertEqual(actions,['admit_write','settle_write','admit_write','settle_write'])

    async def test_unconfirmed_post_blocks_further_rules_and_handover(self):
        self.api.rules = AsyncMock(side_effect=[[],[]])
        result = await self.reconcile()
        self.assertEqual(result['state'],'review_required')
        self.assertEqual(self.db.rows[WORDS]['state'],'uncertain')
        self.assertTrue(self.lease.lost.is_set())
        self.assertNotIn(('coordination','settle_write'), self.calls)

    async def test_audit_failure_does_not_settle_acknowledged_post(self):
        self.db.fail_audit=True
        await self.reconcile()
        self.assertEqual(self.db.rows[WORDS]['state'],'uncertain')
        self.assertTrue(self.lease.lost.is_set())
        self.assertNotIn(('coordination','settle_write'), self.calls)

    async def test_timeout_never_retries(self):
        self.send.side_effect=TimeoutError()
        await self.reconcile()
        self.api.rules=AsyncMock(return_value=[])
        await self.reconcile()
        self.send.assert_awaited_once()
        self.assertTrue(self.lease.lost.is_set())

    async def test_budget_refusal_before_post_can_remain_pending(self):
        self.api.request.side_effect=c.BudgetDeferred('synthetic')
        result=await self.reconcile()
        self.assertEqual(result['state'],'waiting_for_api_budget')
        self.assertEqual(self.db.rows[WORDS]['state'],'pending')
        self.assertFalse(self.lease.lost.is_set())
        self.send.assert_not_awaited()

    async def test_budget_refusal_after_post_is_uncertain(self):
        self.api.rules=AsyncMock(side_effect=[[],c.BudgetDeferred('synthetic')])
        await self.reconcile()
        self.assertEqual(self.db.rows[WORDS]['state'],'uncertain')
        self.assertTrue(self.lease.lost.is_set())
        self.send.assert_awaited_once()

    async def test_cancellation_preserves_audit_and_unresolved_write(self):
        self.send.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.reconcile()
        self.assertEqual(self.db.rows[WORDS]['state'],'uncertain')
        self.assertTrue(self.lease.lost.is_set())
        self.assertNotIn(('coordination','settle_write'),self.calls)

    async def test_drain_finishes_current_rule_without_next_post(self):
        stop=asyncio.Event()
        first={**PAYLOAD,'ID':RULE}
        async def send():
            stop.set()
            return SimpleNamespace(status_code=201,headers={})
        self.send.side_effect=send
        other={**PAYLOAD,'Words':'another-reference'}
        self.api.rules=AsyncMock(side_effect=[[],[first]])
        result=await task_drain.run(stop,self.reconcile,
            {**PROPOSALS,other['Words']:{'payload':other,'tax_bucket':'btw'}})
        self.assertEqual(result['state'],'draining')
        self.send.assert_awaited_once()
        self.assertEqual(self.calls[-1],('coordination','settle_write'))


class CleanupStore:
    def __init__(self, previous=None, locked=True):
        self.state=previous
        self.locked=locked
        self.result=None
        self.queries=[]
    def execute(self, sql, args=()):
        self.queries.append(sql)
        if 'pg_try_advisory_lock' in sql: self.result=(self.locked,)
        elif sql.startswith('SELECT state'): self.result=(self.state,) if self.state else None
        elif sql.startswith('INSERT'): self.state=self.state or args[4]
        elif sql.startswith('UPDATE jnp_rule_cleanup_audit'):
            self.state='deleting' if "state='deleting'" in sql else args[0]
        return self
    def fetchone(self): return self.result


class TaxCleanupTests(unittest.IsolatedAsyncioTestCase):
    setUp = OwnedTaxTests.setUp
    database = OwnedTaxTests.database
    write = OwnedTaxTests.write
    async def cleanup(self, db, collection):
        rule=dict(ID='2a9fa4f3-56eb-462b-843a-919b4fed9e83',Account=ACCOUNT,
                  AccountBankAccount='NL04RABO0200112244')
        self.api.allowed_deletes=set()
        self.api.rules=AsyncMock(side_effect=collection(rule))
        with patch.object(c,'_budget_database_call',side_effect=self.database), f.owner_scope(self.lease):
            return await writes.retire_confirmed_rule(db,self.api,rule,ACCOUNT)

    async def test_delete_is_fenced_and_settles_after_absence(self):
        db=CleanupStore()
        result=await self.cleanup(db,lambda rule:[[rule],[]])
        self.assertEqual(result,'deleted')
        self.assertEqual(db.state,'deleted')
        self.send.assert_awaited_once()
        self.assertEqual(self.calls[-1],('coordination','settle_write'))

    async def test_delete_timeout_keeps_intent_and_does_not_settle(self):
        db=CleanupStore();self.send.side_effect=TimeoutError()
        with self.assertRaises(writes.ConfirmationRequired):
            await self.cleanup(db,lambda rule:[[rule]])
        self.assertEqual(db.state,'uncertain')
        self.assertTrue(self.lease.lost.is_set())
        self.assertNotIn(('coordination','settle_write'),self.calls)
        self.assertTrue(any('pg_advisory_unlock' in q for q in db.queries))

    async def test_historical_uncertainty_survives_operator_edit_without_retry(self):
        db=CleanupStore('uncertain')
        with self.assertRaises(writes.ConfirmationRequired):
            await self.cleanup(db,lambda rule:[[{**rule,'Words':'operator-edit'}]])
        self.assertEqual(db.state,'uncertain')
        self.send.assert_not_awaited()

    async def test_another_role_holding_rule_lock_blocks_delete(self):
        result=await self.cleanup(CleanupStore(locked=False),lambda rule:[])
        self.assertEqual(result,'busy_keep')
        self.api.rules.assert_not_awaited()
        self.send.assert_not_awaited()


class TaxLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopped_cycle_opens_no_database(self):
        stop=asyncio.Event();stop.set();app=MagicMock()
        await task_drain.run(stop,tax_agent.cycle,app)
        app._db_connect.assert_not_called()

    async def test_partial_paginated_scan_never_returns_partial_results(self):
        stop=asyncio.Event()
        api=tax_agent.TaxAPI(MagicMock(DIVISION=3977752, BASE_URL='https://start.exactonline.nl', COLLECTIVE_DEBTOR_CODE='100100'))
        async def get(*args,**kwargs):
            stop.set()
            return {'d':{'results':[{'Code':'1'}],'__next':args[1]+'?skiptoken=next'}}
        with patch.object(tax_agent.transport.Exact,'request',side_effect=get) as request:
            with self.assertRaises(tax_agent.ScanDrained):
                await task_drain.run(stop,api.rows,'financial/GLAccounts')
        self.assertEqual(request.await_count,1)

    async def test_signal_uses_tax_supervisor(self):
        from app import worker
        import signal
        loop=asyncio.get_running_loop();handlers={}
        async def supervise(app,stop,owner,fn,**kwargs):
            self.assertEqual((owner,kwargs),('worker-tax',{'role':'tax'}))
            handlers[signal.SIGTERM]()
            self.assertTrue(stop.is_set())
        with patch('operations.assigned_role.supervise',side_effect=supervise), \
             patch.object(loop,'add_signal_handler',side_effect=lambda sig,fn:handlers.update({sig:fn})), \
             patch.object(loop,'remove_signal_handler') as remove:
            await worker.run_tax(object())
        self.assertEqual(remove.call_count,2)

    async def test_status_comes_from_database_not_process_cache(self):
        from app import main
        conn=MagicMock()
        conn.execute.return_value.fetchone.return_value=(False,{'state':'persisted'},{'counts':{'confirmed':7}})
        with patch.object(main,'DATABASE_URL','synthetic'), \
             patch.object(main,'_db_connect',return_value=nullcontext(conn)), \
             patch.object(c,'status_snapshot',return_value={'roles':[{'role':'tax','lease_live':False}]}), \
             patch.dict(tax_agent.STATUS,{'state':'wrong-memory-value'}):
            result=await tax_agent.status()
        self.assertEqual(result['state'],'persisted')
        self.assertFalse(result['enabled'])
        self.assertEqual(result['allocation_rules']['counts'],{'confirmed':7})
        self.assertFalse(result['worker']['lease_live'])
        self.assertIn('SET default_transaction_read_only=on',[v.args[0] for v in conn.execute.call_args_list])
