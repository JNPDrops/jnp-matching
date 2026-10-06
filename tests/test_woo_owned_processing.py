"""Only synthetic POSTs/collections; no Exact or production connections."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from operations import woo_iban_rules as woo, worker_coordination as c, worker_write_fence as f
from operations.test_woo_iban_rules import body, reference_body, rule, words_rule, ACCOUNT


class Store:
    def __init__(self, calls, fail_confirmation=False):
        self.calls = calls
        self.fail_confirmation = fail_confirmation
    def execute(self, sql, args):
        if 'jnp_woo_rule_attempts' in sql:
            self.calls.append(('audit', args[2], args[0]))
            if self.fail_confirmation and args[2] == 'confirmed':
                raise OSError('synthetic audit failure')
        elif "state='done'" in sql: self.calls.append(('queue', 'done'))
        elif "state='pending'" in sql: self.calls.append(('queue', 'pending'))
        elif "state='creating'" in sql: self.calls.append(('queue', 'creating'))
        elif "state='uncertain'" in sql: self.calls.append(('queue', 'uncertain'))


class OwnedWooTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.lease = SimpleNamespace(database_url='synthetic', division=3977752, role='woo-rules',
            owner='legacy-woo-rules', lease_id=str(uuid4()), lost=asyncio.Event(), _stop=asyncio.Event())
        self.app = SimpleNamespace(DATABASE_URL='synthetic', DIVISION=3977752)
        self.store = Store(self.calls)
        self.response = SimpleNamespace(status_code=201, headers={})
        self.send = AsyncMock(return_value=self.response)
        self.api = SimpleNamespace(account=AsyncMock(return_value=ACCOUNT),
            rules=AsyncMock(side_effect=[[],[rule()]]),
            create=AsyncMock(side_effect=self.write), create_words=AsyncMock(side_effect=self.write))

    async def write(self, *_):
        return await c.budgeted_http(self.app, 'woo-rules', 'POST', self.send)

    def database(self, url, function, *args, **kwargs):
        self.calls.append(('coordination', function.__name__))
        if function is c.reserve_request:
            return c.Reservation(str(uuid4()),3977752,'main','woo-rules','POST')

    async def process(self, previous='pending', data=None):
        with patch.object(c, '_budget_database_call', side_effect=self.database), f.owner_scope(self.lease):
            await woo.process(self.store,self.api,'synthetic-event',data or body(),previous)

    async def test_both_rule_types_settle_only_after_readback_and_durable_audit(self):
        for data, confirmed in [(body(),rule()),(reference_body(),words_rule())]:
            self.calls.clear()
            self.api.rules = AsyncMock(side_effect=[[],[confirmed]])
            self.send.reset_mock()
            await self.process(data=data)
            self.assertEqual(self.calls[-1],('coordination','settle_write'))
            self.assertIn(('queue','done'),self.calls)
            audits=[call for call in self.calls if call[0]=='audit']
            self.assertEqual([a[1] for a in audits],['intent','confirmed'])
            self.assertEqual(audits[0][2],audits[1][2])
            self.send.assert_awaited_once()
            self.assertFalse(self.lease.lost.is_set())

    async def test_successful_post_without_readback_stays_unresolved(self):
        self.api.rules = AsyncMock(side_effect=[[],[]])
        await self.process()
        self.send.assert_awaited_once()
        self.assertNotIn(('coordination','settle_write'),self.calls)
        self.assertNotIn(('queue','done'),self.calls)
        self.assertTrue(self.lease.lost.is_set())

    async def test_audit_failure_after_confirmed_post_stops_owner_without_settlement(self):
        self.store.fail_confirmation=True
        await self.process()
        self.send.assert_awaited_once()
        self.assertNotIn(('coordination','settle_write'),self.calls)
        self.assertTrue(self.lease.lost.is_set())
        self.assertIn(('queue','uncertain'),self.calls)

    async def test_transport_error_never_retries(self):
        self.send.side_effect=TimeoutError('synthetic')
        await self.process()
        self.send.assert_awaited_once()
        self.assertNotIn(('coordination','settle_write'),self.calls)
        self.assertTrue(self.lease.lost.is_set())

    async def test_post_budget_refusal_stays_pending_without_losing_owner(self):
        self.api.create.side_effect=c.BudgetDeferred('synthetic')
        await self.process()
        self.send.assert_not_awaited()
        self.assertIn(('queue','pending'),self.calls)
        self.assertNotIn(('queue','uncertain'),self.calls)
        self.assertFalse(self.lease.lost.is_set())

    async def test_readback_budget_refusal_after_post_is_uncertain(self):
        self.api.rules = AsyncMock(side_effect=[[],c.BudgetDeferred('synthetic')])
        await self.process()
        self.send.assert_awaited_once()
        self.assertIn(('queue','uncertain'),self.calls)
        self.assertNotIn(('queue','pending'),self.calls)
        self.assertNotIn(('coordination','settle_write'),self.calls)
        self.assertTrue(self.lease.lost.is_set())

    async def test_cancellation_retains_intent(self):
        self.send.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.process()
        self.assertNotIn(('coordination','settle_write'),self.calls)
        self.assertTrue(self.lease.lost.is_set())
        self.send.assert_awaited_once()

    async def test_existing_and_historically_uncertain_events_never_create_again(self):
        for previous, collection in [('pending',[rule()]),('creating',[]),('uncertain',[])]:
            self.calls.clear()
            self.api.rules=AsyncMock(return_value=collection)
            await self.process(previous)
            self.assertFalse(any(call[0]=='coordination' for call in self.calls))
        self.send.assert_not_awaited()
        self.api.create.assert_not_awaited()
