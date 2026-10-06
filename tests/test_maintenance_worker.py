import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4

from operations import maintenance_write_operations as writes, allocation_maintenance as m
from operations import worker_coordination as c, worker_write_fence as f, task_drain

RULE='00000000-0000-0000-0000-000000000001'
PAYLOAD={'Words':'synthetic-reference','Account':'00000000-0000-0000-0000-000000000002'}


class Store:
    def __init__(self):self.calls=[];self.fail_audit=False
    def execute(self,sql,args=()):
        if 'jnp_maintenance_rule_attempts' in sql:
            self.calls.append(('audit',args[3],args[0]))
            if self.fail_audit and args[3]=='confirmed':raise OSError('synthetic')
        elif 'jnp_suspense_rules' in sql:self.calls.append(('state',args[0]))


class MaintenanceWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=Store();self.events=self.db.calls
        self.lease=SimpleNamespace(database_url='synthetic',division=3977752,role='maintenance',
            owner='legacy-maintenance',lease_id=str(uuid4()),lost=asyncio.Event(),_stop=asyncio.Event())
        self.app=SimpleNamespace(DATABASE_URL='synthetic',DIVISION=3977752)
        self.send=AsyncMock(return_value=SimpleNamespace(status_code=201,headers={}))
        async def request(method,*args,**kwargs):return await c.budgeted_http(self.app,'maintenance',method,self.send)
        self.api=SimpleNamespace(allowed_posts={},rules=AsyncMock(return_value=[{**PAYLOAD,'ID':RULE}]),request=request)
    def database(self,url,function,*args,**kwargs):
        self.events.append(('coordination',function.__name__))
        if function is c.reserve_request:return c.Reservation(str(uuid4()),3977752,'main','maintenance','POST')
    async def create(self):
        with patch.object(c,'_budget_database_call',side_effect=self.database),f.owner_scope(self.lease):
            return await writes.create_confirmed_rule(self.db,self.api,PAYLOAD)
    async def test_confirmed_rule_settles_after_durable_audit(self):
        self.assertEqual(await self.create(),'confirmed')
        self.assertEqual(self.events[-1],('coordination','settle_write'))
        self.assertEqual(self.events[-2][0:2],('audit','confirmed'))
        self.assertEqual(self.api.allowed_posts,{})
    async def test_conflicting_recognition_words_stop_owner(self):
        self.api.rules.return_value=[{**PAYLOAD,'ID':RULE},{**PAYLOAD,'ID':str(uuid4()),'GLAccount':RULE}]
        with self.assertRaises(writes.RuleUnconfirmed):await self.create()
        self.assertTrue(self.lease.lost.is_set())
        self.assertIn(('state','uncertain'),self.events)
        self.assertNotIn(('coordination','settle_write'),self.events)
    async def test_transport_timeout_and_cancellation_never_settle(self):
        for error in [TimeoutError(),asyncio.CancelledError()]:
            self.setUp();self.send.side_effect=error
            with self.assertRaises(type(error)):await self.create()
            self.assertTrue(self.lease.lost.is_set())
            self.send.assert_awaited_once()
            self.assertNotIn(('coordination','settle_write'),self.events)
    async def test_failed_confirmation_audit_stops_owner(self):
        self.db.fail_audit=True
        with self.assertRaises(OSError):await self.create()
        self.assertTrue(self.lease.lost.is_set())
        self.assertNotIn(('coordination','settle_write'),self.events)
    async def test_pre_send_budget_deferral_is_pending(self):
        self.api.request=AsyncMock(side_effect=c.BudgetDeferred('synthetic'))
        self.assertEqual(await self.create(),'pending')
        self.assertFalse(self.lease.lost.is_set())
        self.send.assert_not_awaited()
    async def test_post_send_budget_deferral_is_uncertain(self):
        self.api.rules.side_effect=c.BudgetDeferred('synthetic')
        with self.assertRaises(c.BudgetDeferred):await self.create()
        self.assertTrue(self.lease.lost.is_set())
        self.assertIn(('state','uncertain'),self.events)
    async def test_delete_finishes_business_checkpoint_before_release(self):
        async def delete(*args):
            await self.api.request('DELETE')
            return 'deleted'
        with patch.object(m,'delete_rule',side_effect=delete),patch.object(c,'_budget_database_call',side_effect=self.database),f.owner_scope(self.lease):
            await writes.delete_confirmed_rule(self.db,self.api,{'ID':RULE},'synthetic',
                checkpoint=lambda:self.events.append(('checkpoint','retired')))
        self.assertIn(('checkpoint','retired'),self.events)
        self.assertEqual(self.events[-1],('coordination','settle_write'))
    async def test_delete_checkpoint_failure_leaves_unresolved_write(self):
        async def delete(*args):
            await self.api.request('DELETE');return 'deleted'
        def checkpoint():raise OSError('synthetic')
        with patch.object(m,'delete_rule',side_effect=delete),patch.object(c,'_budget_database_call',side_effect=self.database),f.owner_scope(self.lease):
            with self.assertRaises(OSError):
                await writes.delete_confirmed_rule(self.db,self.api,{'ID':RULE},'synthetic',checkpoint=checkpoint)
        self.assertTrue(self.lease.lost.is_set())
        self.assertNotIn(('coordination','settle_write'),self.events)
    async def test_drained_cycle_never_opens_database(self):
        stop=asyncio.Event();stop.set();app=MagicMock()
        await task_drain.run(stop,m.cycle,app)
        app._db_connect.assert_not_called()

