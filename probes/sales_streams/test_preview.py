import copy
import unittest
from preview import preview


def order(**kw):
    return dict(id=900001, number='900001', payment_method='demo_bank',
                status='completed', currency='EUR', total='12.50', refunds=[], **kw)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.mapping = {'demo_bank': {'stream': 'synthetic_bank', 'debtor_code': 'DEMO_ONLY', 'confirmed': True}}

    def test_confirmed_route_is_still_never_writable(self):
        r = preview([order()], self.mapping)['orders'][0]
        self.assertEqual(r['routing_status'], 'ROUTING_PROPOSAL')
        self.assertFalse(r['exact_write_allowed'])
        self.assertEqual(r['exact_comparison'], 'NOT_PERFORMED')

    def test_unknown_code_has_no_fallback_to_100100(self):
        r = preview([order()], {})['orders'][0]
        self.assertIsNone(r['proposed_debtor_code'])
        self.assertIn('UNKNOWN_PAYMENT_METHOD', r['issues'])

    def test_unconfirmed_and_missing_routes_block(self):
        m = {'demo_bank': {'confirmed': False}}
        r = preview([order()], m)['orders'][0]
        self.assertIn('ROUTING_NOT_CONFIGURED', r['issues'])
        self.assertIn('ROUTING_NOT_CONFIRMED', r['issues'])

    def test_duplicate_orders_are_all_flagged(self):
        rows = preview([order(), order()], self.mapping)['orders']
        self.assertTrue(all('DUPLICATE_ORDER_ID_OR_NUMBER' in r['issues'] for r in rows))

    def test_refunds_and_missing_refund_information(self):
        o = order(); o['refunds'] = [{'id': 42, 'total': '-2.00'}]
        self.assertIn('REFUND_REQUIRES_CREDIT_REVIEW', preview([o], self.mapping)['orders'][0]['issues'])
        del o['refunds']
        self.assertIn('REFUND_INFORMATION_MISSING', preview([o], self.mapping)['orders'][0]['issues'])

    def test_currency_status_and_amount_guards(self):
        for value in (None, 'NaN', 'Infinity', '12.501', 'bad'):
            with self.subTest(value=value):
                o = order(); o.update(total=value, currency='USD', status='cancelled')
                issues = preview([o], self.mapping)['orders'][0]['issues']
                self.assertIn('INVALID_TOTAL', issues)
                self.assertIn('CURRENCY_REQUIRES_REVIEW', issues)
                self.assertIn('ORDER_STATUS_REQUIRES_REVIEW', issues)

    def test_no_customer_details_and_no_input_mutation(self):
        o = order(); o['billing'] = {'email': 'private@example.invalid'}
        before = copy.deepcopy(o)
        result = preview([o], self.mapping)
        self.assertNotIn('private@example.invalid', str(result))
        self.assertEqual(o, before)

    def test_same_customer_can_use_different_routes(self):
        a = order(); b = order(); b.update(id=900002, number='900002', payment_method='demo_psp')
        a['customer_id'] = b['customer_id'] = 99
        self.mapping['demo_psp'] = {'stream': 'synthetic_psp', 'debtor_code': 'DEMO_PSP', 'confirmed': True}
        rows = preview([a, b], self.mapping)['orders']
        self.assertEqual([r['proposed_debtor_code'] for r in rows], ['DEMO_ONLY', 'DEMO_PSP'])


if __name__ == '__main__':
    unittest.main()
