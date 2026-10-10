import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from operations import icepay_automatic as automatic, icepay_jobs
from operations import worker_coordination as coordination, worker_write_fence as fence, routing_completion


def snapshot():
    cells = ['', '06-10-2026', '', 'EUR', '100,00', '', '',
             '1100 - Debiteuren', '109419 - ICEPAY', '', 'EUR', '', '', '', '', '']
    return {'controls': [{'id': 'BankAccount', 'value': '{'+automatic.BANK+'}'},
                         {'id': 'Notes', 'value': automatic.NOTES},
                         {'id': 'Status1', 'checked': True}, {'id': 'Status2', 'checked': False}],
            'rows': [{'cells': cells}, {'cells': ['ICEPAY TD123 | Payment 555', '', '', '', '', '']}]}


class ReceiptScopeTests(unittest.TestCase):
    def test_receipt_identity_and_amount(self):
        self.assertEqual(automatic.checked_rows(snapshot()),
                         [{'order': 'TD123', 'payment_id': '555', 'amount': '100.00'}])

    def test_other_bank_or_missing_filter_rejected(self):
        for key, value in [('BankAccount', 'other'), ('Notes', '')]:
            data = snapshot()
            next(c for c in data['controls'] if c['id'] == key)['value'] = value
            with self.assertRaises(ValueError): automatic.checked_rows(data)

    def test_payout_credit_other_debtor_and_cross_account_rejected(self):
        for index, value in [(4, '-100,00'), (7, '1360 - Kruisposten'),
                             (8, '100100 - TD'), (3, 'USD')]:
            data = snapshot(); data['rows'][0]['cells'][index] = value
            with self.assertRaises(ValueError): automatic.checked_rows(data)
        data = snapshot(); data['rows'][1]['cells'][0] = 'ICEPAY Payout'
        with self.assertRaises(ValueError): automatic.checked_rows(data)

    def test_no_arbitrary_action_or_parameters(self):
        for action, params in [('match', {}), ('run', {'bank': 'other'})]:
            with self.assertRaises(ValueError):
                icepay_jobs.validate({'task_key': automatic.TASK, 'action': action, 'params': params})

    def test_seed_respects_unknown_click(self):
        conn = MagicMock(); conn.execute.return_value.fetchone.side_effect = [None, (True,)]
        with patch.object(automatic.agent_jobs, 'submit') as submit:
            automatic.seed(conn, SimpleNamespace(DIVISION=3977752))
        submit.assert_not_called()

    def test_seed_waits_between_completed_rounds(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [None, None, (datetime.now(timezone.utc),)]
        with patch.object(automatic.agent_jobs, 'submit') as submit:
            automatic.seed(conn, SimpleNamespace(DIVISION=3977752))
        submit.assert_not_called()


class NativeActionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, failed_readback=False, routing_blocked=False):
        events = []
        app = SimpleNamespace(DATABASE_URL='synthetic', DIVISION=3977752)
        lease = SimpleNamespace(database_url='synthetic', division=3977752, role='icepay',
                                lease_id=str(uuid4()), owner='worker-icepay',
                                lost=asyncio.Event(), _stop=asyncio.Event())
        page = MagicMock()
        page.locator.return_value.click = AsyncMock(side_effect=lambda: events.append('native_click'))
        page.wait_for_load_state = AsyncMock()
        async def readback(page):
            events.append('readback')
            if failed_readback: raise ValueError('readback_failed')
            return snapshot(), [{'order': 'TD123'}]
        def persist(app, job, state, summary, evidence): events.append(state)
        def routing_check(*args):
            events.append('routing_checked')
            if routing_blocked:
                raise routing_completion.RoutingNotReady('no_successful_routing_cycle')
            return {'id': 1}
        with fence.owner_scope(lease), \
             patch.object(automatic, 'require_native_allowed'), \
             patch.object(routing_completion, 'check_app', side_effect=routing_check), \
             patch.object(automatic, 'persist', side_effect=persist), \
             patch.object(automatic, 'open_receipts', side_effect=readback), \
             patch.object(coordination, '_budget_database_call', side_effect=lambda url, fn, *a: events.append(fn.__name__)):
            try:
                result = await automatic.automatic_click(app, {'job_id': str(uuid4())}, page, snapshot(), {})
            except ValueError:
                result = None
        return events, lease, result, page

    async def test_native_button_only_settled_after_durable_readback(self):
        events, lease, result, page = await self.exercise()
        self.assertEqual(events, ['routing_checked', 'click_requested', 'admit_write', 'native_click', 'readback', 'completed', 'settle_write'])
        self.assertEqual(result['remaining_open'], 1)
        page.locator.assert_called_once_with('#btnAutomatic')
        self.assertFalse(lease.lost.is_set())

    async def test_unfinished_routing_prevents_native_click(self):
        events, lease, result, page = await self.exercise(routing_blocked=True)
        self.assertEqual(events, ['routing_checked'])
        self.assertIsNone(result)
        page.locator.return_value.click.assert_not_awaited()
        self.assertFalse(lease.lost.is_set())

    async def test_unknown_result_blocks_replay(self):
        events, lease, result, page = await self.exercise(True)
        self.assertNotIn('settle_write', events)
        self.assertTrue(lease.lost.is_set())
        page.locator.return_value.click.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
