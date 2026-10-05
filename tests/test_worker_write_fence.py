import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from operations import worker_coordination as c
from operations import worker_write_fence as f


def owner():
    return SimpleNamespace(database_url='test-only', division=3977752, role='routing',
        owner='test-owner', lease_id=str(uuid4()), lost=asyncio.Event(), _stop=asyncio.Event())


class AdmissionTests(unittest.TestCase):
    def test_stale_expired_draining_or_missing_owner_cannot_admit(self):
        lease = owner()
        for row in (None, ('other-owner', lease.lease_id, False, True),
                    (lease.owner, uuid4(), False, True),
                    (lease.owner, lease.lease_id, True, True),
                    (lease.owner, lease.lease_id, False, False)):
            with self.subTest(row=row):
                conn = MagicMock()
                conn.transaction.return_value = nullcontext()
                conn.execute.return_value.fetchone.return_value = row
                with self.assertRaisesRegex(f.WriteFenced, 'no_longer_owns'):
                    f.admit_write(conn, f.Operation(str(uuid4()), lease), 'main', 'PUT')
                self.assertFalse(any('INSERT' in call.args[0] for call in conn.execute.call_args_list))

    def test_unresolved_prior_write_blocks_even_same_owner(self):
        lease = owner()
        conn = MagicMock()
        conn.transaction.return_value = nullcontext()
        conn.execute.return_value.fetchone.side_effect = [
            (lease.owner, lease.lease_id, False, True), (1,)]
        with self.assertRaisesRegex(f.WriteFenced, 'prior_write_requires_review'):
            f.admit_write(conn, f.Operation(str(uuid4()), lease), 'allocation', 'PUT')


class OwnedOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lease = owner()
        self.app = SimpleNamespace(DATABASE_URL='test-only', DIVISION=3977752)
        self.response = SimpleNamespace(status_code=204, headers={})
        self.calls = []

    def db(self, database_url, function, *args, **kwargs):
        self.calls.append(function.__name__)
        if function is c.reserve_request:
            return c.Reservation(str(uuid4()), 3977752, 'main', 'routing', 'PUT')

    async def test_audit_finishes_before_intent_is_settled(self):
        send = AsyncMock(return_value=self.response)
        @f.owned_operation('routing')
        async def operation():
            await c.budgeted_http(self.app, 'routing', 'PUT', send)
            self.calls.append('durable_audit')
            return 'done'
        with patch.object(c, '_budget_database_call', side_effect=self.db), f.owner_scope(self.lease):
            self.assertEqual(await operation(), 'done')
        self.assertEqual(self.calls, ['reserve_request', 'admit_write', 'complete_request',
                                     'durable_audit', 'settle_write'])
        send.assert_awaited_once()

    async def test_audit_failure_retains_intent_and_never_retries(self):
        send = AsyncMock(return_value=self.response)
        @f.owned_operation('routing')
        async def operation():
            await c.budgeted_http(self.app, 'routing', 'PUT', send)
            raise OSError('synthetic audit failure')
        with patch.object(c, '_budget_database_call', side_effect=self.db), f.owner_scope(self.lease):
            with self.assertRaises(OSError):
                await operation()
        self.assertNotIn('settle_write', self.calls)
        send.assert_awaited_once()

    async def test_transport_failure_cancellation_and_5xx_retain_intent(self):
        for error in (OSError('test'), asyncio.CancelledError(), None):
            self.calls.clear()
            send = AsyncMock(side_effect=error, return_value=SimpleNamespace(status_code=503, headers={}))
            @f.owned_operation('routing')
            async def operation():
                return await c.budgeted_http(self.app, 'routing', 'PUT', send)
            with patch.object(c, '_budget_database_call', side_effect=self.db), f.owner_scope(self.lease):
                if error:
                    with self.assertRaises(type(error)):
                        await operation()
                else:
                    await operation()
            self.assertNotIn('settle_write', self.calls)
            send.assert_awaited_once()

    async def test_denied_admission_does_not_send(self):
        send = AsyncMock()
        def deny(database_url, function, *args, **kwargs):
            if function is f.admit_write:
                raise f.WriteFenced('lost')
            return self.db(database_url, function, *args, **kwargs)
        @f.owned_operation('routing')
        async def operation():
            await c.budgeted_http(self.app, 'routing', 'PUT', send)
        with patch.object(c, '_budget_database_call', side_effect=deny), f.owner_scope(self.lease):
            with self.assertRaises(f.WriteFenced):
                await operation()
        send.assert_not_awaited()

    async def test_owned_writer_requires_adapter_and_matching_database(self):
        send = AsyncMock()
        with patch.object(c, '_budget_database_call', side_effect=self.db), f.owner_scope(self.lease):
            with self.assertRaises(f.WriteFenced):
                await c.budgeted_http(self.app, 'routing', 'PUT', send)
            for database, division in (('',3977752), ('other',3977752), ('test-only',42)):
                with self.assertRaisesRegex(f.WriteFenced, 'scope_mismatch'):
                    await c.budgeted_http(SimpleNamespace(DATABASE_URL=database, DIVISION=division),
                                         'routing', 'PUT', send)
        send.assert_not_awaited()

    async def test_owner_context_is_inherited_without_leaking(self):
        async def child():
            return f._owner.get()
        with f.owner_scope(self.lease):
            task = asyncio.create_task(child())
        self.assertIsNone(f._owner.get())
        self.assertIs(await task, self.lease)

    async def test_second_write_in_one_operation_is_not_sent(self):
        send = AsyncMock(return_value=self.response)
        @f.owned_operation('routing')
        async def operation():
            await c.budgeted_http(self.app, 'routing', 'PUT', send)
            await c.budgeted_http(self.app, 'routing', 'PUT', send)
        with patch.object(c, '_budget_database_call', side_effect=self.db), f.owner_scope(self.lease):
            with self.assertRaises(f.WriteFenced):
                await operation()
        send.assert_awaited_once()
        self.assertNotIn('settle_write', self.calls)

    async def test_lost_owner_after_admission_never_sends(self):
        send = AsyncMock()
        def lose(database_url, function, *args, **kwargs):
            if function is f.admit_write:
                self.lease.lost.set()
            return self.db(database_url, function, *args, **kwargs)
        @f.owned_operation('routing')
        async def operation():
            await c.budgeted_http(self.app, 'routing', 'PUT', send)
        with patch.object(c, '_budget_database_call', side_effect=lose), f.owner_scope(self.lease):
            with self.assertRaises(f.WriteFenced):
                await operation()
        send.assert_not_awaited()
        self.assertNotIn('settle_write', self.calls)

    async def test_real_routing_adapter_persists_correlated_audit_before_settlement(self):
        from operations import customer_only_routing as routing
        from operations.test_customer_only_routing import ACCOUNTS, HEADER, OPEN, SELECTION
        send = AsyncMock(return_value=self.response)
        api = AsyncMock()
        api.limits = {'remaining': 500}
        api.rows.side_effect = [[dict(HEADER)], [dict(OPEN)]]
        async def write(*args):
            await c.budgeted_http(self.app, 'routing', 'PUT', send)
        api.change_customer.side_effect = write
        events = []
        def persist(event):
            events.append(event)
            self.calls.append(event['event'])
        audit = SimpleNamespace(persist_event=persist)
        with patch.object(c, '_budget_database_call', side_effect=self.db), f.owner_scope(self.lease):
            result = await routing.change_selected(api, SELECTION, ACCOUNTS, audit)
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(self.calls[-2:], ['customer_applied', 'settle_write'])
        self.assertEqual(events[0]['worker_operation_id'], events[-1]['worker_operation_id'])
        send.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
