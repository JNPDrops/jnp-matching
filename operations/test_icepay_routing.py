import json
from contextlib import nullcontext
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import icepay_routing as i, automatic_debtor_routing as a
from operations import customer_only_routing as c, bacs_debtor_transfer as m
from operations.test_customer_only_routing import HEADER, OPEN, ID, ACCOUNTS, Audit

TARGET = '00000000-0000-0000-0000-000000000419'
ORDER = {'order_id': 137196, 'order_number': '#48874', 'payment_method': i.METHOD}
SOURCE = ACCOUNTS['100100']


class IcepayTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_open_entry_sends_only_customer_and_never_rereads(self):
        api = AsyncMock(); api.limits = {'remaining': 500}
        api.rows.side_effect = [[HEADER], [{**OPEN, 'Amount': 0.01}]]
        audit = Audit()
        result = await c.change_selected(api, {'entry_id': ID, 'reference': 'TD48874',
            'order_id': 137196, 'payment_method': i.METHOD}, {**ACCOUNTS, '109419': TARGET}, audit)
        self.assertEqual(result['state'], 'applied')
        api.change_customer.assert_awaited_once_with(ID, TARGET)
        self.assertEqual(api.rows.await_count, 2)
        self.assertEqual(audit.events[0]['payload'], {'Customer': TARGET})

    async def test_icepay_paid_and_wrong_debtor_are_not_changed(self):
        for header, opens in ((HEADER, []), ({**HEADER, 'Customer': ACCOUNTS['109377']}, [OPEN])):
            api = AsyncMock(); api.limits = {'remaining': 500}; api.rows.side_effect = [[header], opens]
            await c.change_selected(api, {'entry_id': ID, 'reference': 'TD48874',
                'order_id': 137196, 'payment_method': i.METHOD}, {**ACCOUNTS, '109419': TARGET}, Audit())
            api.change_customer.assert_not_awaited()

    async def test_retained_and_unknown_codes_are_not_inferred(self):
        for method, expected in (('wc_fibonatix', 'retained'), ('wc_fibonatics', 'retained'),
                                 ('icepay-unknown', 'skipped'), ('np_payments','skipped')):
            conn = MagicMock(); api = AsyncMock()
            with patch.object(c, 'change_selected', AsyncMock()) as change:
                await a.process_entry(api, conn, ID, 'TD48874', {**ORDER, 'payment_method': method})
            change.assert_not_awaited()
            self.assertEqual(conn.execute.call_args.args[1][0], expected)

    async def test_missing_destination_blocks_processing(self):
        api = AsyncMock()
        api.rows.return_value = [{'ID': SOURCE, 'Code': '100100', 'IsSales': True, 'Status': 'C'}]
        with patch.object(c, '_accounts', None):
            with self.assertRaises(m.Stop): await a.validate_routes(api)
        api.change_customer.assert_not_awaited()

    async def test_last_write_guard_rejects_other_destinations(self):
        app = MagicMock(DIVISION=m.DIVISION, BASE_URL=m.BASE, COLLECTIVE_DEBTOR_CODE=m.SOURCE)
        conn = MagicMock(); conn.execute.return_value.fetchone.return_value = (True,)
        api = a.AutomaticExact(app, conn)
        with patch.object(c, 'route_accounts', AsyncMock(return_value={'100100': SOURCE, '109419': TARGET})), patch.object(m.Exact, 'change_customer', AsyncMock()) as write:
            with self.assertRaises(m.Stop): await api.change_customer(ID, ACCOUNTS['109377'])
        write.assert_not_awaited()

    async def test_budget_stops_pagination_or_write_without_new_request(self):
        app = MagicMock(DIVISION=m.DIVISION, BASE_URL=m.BASE, COLLECTIVE_DEBTOR_CODE=m.SOURCE)
        api = a.AutomaticExact(app, MagicMock()); api.limits = {'remaining': 100}
        with patch.object(m.Exact, 'request', AsyncMock()) as request:
            with self.assertRaises(c.BudgetDeferred): await api.request('GET', 'unused')
        request.assert_not_awaited()
        self.assertEqual(c.failure_result(c.BudgetDeferred(), True)['state'], 'pending')

    def test_open_discovery_has_no_age_cutoff_but_rejects_paid_and_duplicate_refs(self):
        rows = [{**OPEN, 'YourRef': 'TD10000'}, {**OPEN, 'YourRef': 'TD20000', 'Amount': 0},
                {**OPEN, 'YourRef': 'TD30000'}, {**OPEN, 'YourRef': 'TD30000'},
                {**OPEN, 'YourRef': 'manual invoice'}]
        self.assertEqual(i.candidates(rows, SOURCE), [{'reference': 'TD10000', 'entry_number': OPEN['EntryNumber']}])

    async def test_discovery_links_live_open_item_to_order_and_preserves_uncertain_queue_entries(self):
        conn = MagicMock(); conn.transaction.return_value = nullcontext()
        conn.execute.return_value.fetchone.return_value = (None,)
        api = AsyncMock()
        api.rows.side_effect = [[OPEN], [{**HEADER, 'Modified': '/Date(0)/'}]]
        with patch.object(i.e, 'lookup_orders', AsyncMock(return_value={'orders': {'#48874': ORDER}})):
            await i.discover_batch(api, conn, SOURCE)
        self.assertNotIn('Date', api.rows.await_args_list[0].args[1]['$filter'])
        inserts = [x for x in conn.execute.call_args_list if 'INSERT INTO jnp_debtor_route_queue' in x.args[0]]
        self.assertEqual(len(inserts), 1)
        self.assertIn("'uncertain'", inserts[0].args[0])
        self.assertEqual(json.loads(inserts[0].args[1][3]), ORDER)
        saved = json.loads(conn.execute.call_args.args[1][0])
        self.assertTrue(saved['done']); self.assertEqual(saved['queued'], 1)
        api.change_customer.assert_not_awaited()

    async def test_missing_or_non_icepay_proof_never_looks_up_sales_entry(self):
        for orders in ({}, {'#48874': {**ORDER, 'payment_method': 'suap_wordpresspayplugin'}}):
            conn = MagicMock(); conn.transaction.return_value = nullcontext()
            conn.execute.return_value.fetchone.return_value = (None,)
            api = AsyncMock(); api.rows.return_value = [OPEN]
            with patch.object(i.e, 'lookup_orders', AsyncMock(return_value={'orders': orders})):
                await i.discover_batch(api, conn, SOURCE)
            self.assertEqual(api.rows.await_count, 1)
            self.assertFalse(any('INSERT INTO jnp_debtor_route_queue' in x.args[0] for x in conn.execute.call_args_list))

    def test_once_only_resume_keeps_cursor_and_never_overrides_later_pause(self):
        now = datetime.now(timezone.utc)
        for resumed in (None, now):
            conn = MagicMock(); conn.transaction.return_value = nullcontext()
            conn.execute.return_value.fetchone.return_value = (now, now, resumed)
            i.resume_once(conn)
            writes = [x.args[0] for x in conn.execute.call_args_list if x.args[0].startswith('UPDATE')]
            self.assertEqual(len(writes), 1 if resumed is None else 0)
            self.assertTrue(all('cursor_at=' not in x and 'route_queue' not in x for x in writes))
