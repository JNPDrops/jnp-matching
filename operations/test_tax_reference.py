import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from operations import tax_reference as t, tax_agent as a


def bank(description='6739305619301120', amount='-100.00', **changes):
    return {'Description': description, 'AmountDC': amount, 'Date': '2026-10-04',
            'AccountName': 'Belastingdienst', **changes}


class TaxReferenceTests(unittest.TestCase):
    def test_confirmed_james_n_parson_reference(self):
        d = t.decode_payment('6739 3056 1930 1120', anchor_year=2026)
        self.assertEqual((d['rsin'], d['tax_bucket'], d['tax_year'], d['assessment_kind'], d['period_code']),
                         ('867393051', 'vpb', 2026, 'voorlopig', '0112'))
        self.assertEqual(d['assessment_number'], '8673.93.051.V.61.0112')

    def test_independent_official_examples(self):
        # Belastingdienst specification v1.5. Payment-reference digits are the
        # fixture authority (the PDF's A/F example year labels are inconsistent).
        fixtures = [('9253586208001120', '802535860', 'V', 2022, '0112'),
                    ('6253586368001230', '802535860', 'V', 2023, '0123'),
                    ('2036000016301110', '036000012', 'L', 2023, '11'),
                    ('0036000011302270', '036000012', 'B', 2023, '27'),
                    ('1036000015303240', '036000012', 'F', 2023, '24'),
                    ('1036000010304121', '036000012', 'A', 2023, '12')]
        for reference, rsin, letter, year, period in fixtures:
            with self.subTest(reference=reference):
                d = t.decode_payment(reference, anchor_year=year, expected_rsin=None)
                self.assertEqual((d['rsin'], d['tax_letter'], d['tax_year'], d['period_code']),
                                 (rsin, letter, year, period))

    def test_check_digit_zero_one_and_published_payment_standard(self):
        self.assertEqual(t.check_digit('000056789012345'), '5')
        self.assertEqual(t.check_digit('000000000000000'), '0')
        self.assertEqual(t.check_digit('000000000000005'), '1')
        self.assertEqual(t.check_digit('000000000000006'), '1')

    def test_typo_wrong_rsin_and_unsupported_types_are_rejected(self):
        for number in ('5739305619301120', '6739305619301121', '9253586208001120',
                       '5000056789012345', '673930561930112', 'x6739305619301120'):
            with self.subTest(number=number), self.assertRaises(t.ReferenceError):
                t.decode_payment(number, anchor_year=2026)

    def test_nearest_year_rollover_and_tie(self):
        for digit, anchor, expected in [(6, 2026, 2026), (9, 2030, 2029), (0, 2029, 2030),
                                       (1, 2026, 2021), (2, 2026, 2022), (6, 2017, 2016)]:
            self.assertEqual(t.nearest_year(digit, anchor), expected)

    def test_bank_date_anchors_historical_payments_not_wall_clock(self):
        d = t.classify_bank_line(bank(Date='2016-09-01'))
        self.assertEqual(d['tax_year'], 2016)
        stamp = int(datetime(2026, 10, 4, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(t.bank_date(f'/Date({stamp})/').isoformat(), '2026-10-04')
        self.assertEqual(t.classify_bank_line(bank(Date=None))['status'], 'REVIEW_TAX')

    def test_formatted_and_unformatted_assessments(self):
        for value in ('8673.93.051.V.61.0112', '867393051V2610112', '867393051V610112'):
            d = t.decode_assessment(value, anchor_year=2026)
            self.assertEqual(d['payment_reference'], '6739305619301120')
        d = t.decode_assessment('036000012B0223270', anchor_year=2023, expected_rsin=None)
        self.assertEqual(d['payment_reference'], '0036000011302270')
        with self.assertRaises(t.ReferenceError):
            t.decode_assessment('867393051V1610112', anchor_year=2026)

    def test_both_directions_preserve_signed_amount_and_refunds_need_breakdown(self):
        out = t.classify_bank_line(bank(amount='-125.10'))
        incoming = t.classify_bank_line(bank(amount='125.10'))
        self.assertEqual((out['direction'], out['amount_dc']), ('outgoing', '-125.10'))
        self.assertEqual((incoming['direction'], incoming['amount_dc']), ('incoming', '125.10'))
        self.assertEqual(out['tax_bucket'], incoming['tax_bucket'])
        self.assertEqual(out['status'], 'TAX_IDENTIFIED')
        self.assertEqual(incoming['status'], 'REVIEW_TAX')
        self.assertFalse(out['booking_executed'])

    def test_interest_penalties_and_collection_are_not_auto_bookable(self):
        for suffix in ('belastingrente', 'boete', 'aanmaningskosten', 'betalingsregeling', 'verrekening'):
            d = t.classify_bank_line(bank('6739305619301120 ' + suffix))
            self.assertEqual(d['status'], 'REVIEW_TAX')
        for letter in ('A', 'F'):
            d = t.classify_bank_line(bank('8673.93.051.' + letter + '.01.6240'))
            self.assertEqual(d['status'], 'REVIEW_TAX')

    def test_multiple_references_invalid_extra_and_duplicate_mentions(self):
        d = t.classify_bank_line(bank('6739305619301120 en 8673.93.051.B.01.6300'))
        self.assertEqual(d['reason'], 'Meerdere aanslagen in één bankbetaling; splitsing nodig')
        d = t.classify_bank_line(bank('6739305619301120 en 5739305619301120'))
        self.assertEqual(d['status'], 'REVIEW_TAX')
        d = t.classify_bank_line(bank('6739305619301120 en 8673.93.051.V.61.0112'))
        self.assertEqual(d['status'], 'TAX_IDENTIFIED')

    def test_missing_random_long_and_fragmented_reference_fail_closed(self):
        for description in ('betaling', '6739 bestelling 3056 bedrag 1930 order 1120',
                            '16739305619301120', '67393056193011201', '5000056789012345'):
            self.assertEqual(t.classify_bank_line(bank(description))['status'], 'REVIEW_TAX')
        self.assertIsNone(t.classify_bank_line(bank('klant 48451', AccountName='Klant')))

    def test_invalid_amounts(self):
        for amount in (None, 'NaN', 'Infinity', '0', True):
            self.assertEqual(t.classify_bank_line(bank(amount=amount))['status'], 'REVIEW_TAX')

    def test_account_proposals_do_not_choose_profit_and_loss_or_blocked_accounts(self):
        accounts = [dict(ID='a', Code='A', Description='Vpb', BalanceType='W', IsBlocked=False),
                    dict(ID='b', Code='B', Description='Vpb', BalanceType='B', IsBlocked=True),
                    dict(ID='c', Code='C', Description='Te betalen Vpb', BalanceType='B', IsBlocked=False)]
        self.assertEqual([x['code'] for x in t.account_candidates(accounts)['vpb']], ['C'])
        decision = t.classify_bank_line(bank())
        self.assertIsNone(t.add_account_proposal(decision, accounts, {})['gl_account_id'])
        self.assertIsNone(t.add_account_proposal(decision, accounts, {'vpb': 'A'})['gl_account_id'])
        self.assertEqual(t.add_account_proposal(decision, accounts, {'vpb': 'C'})['gl_account_id'], 'c')


class TaxIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_supplier_relation_does_not_require_customer_status(self):
        api = AsyncMock()
        relation = dict(ID='00000000-0000-0000-0000-000000000001', Code='                 1',
                        Name='Belastingdienst', IsSupplier=True, EndDate=None)
        api.rows.side_effect = [[], [relation]]
        self.assertEqual((await a.metadata(api))['tax_account_id'], relation['ID'])
        api.rows.side_effect = [[], [{**relation, 'IsSupplier': False}]]
        with self.assertRaises(a.transport.Stop):
            await a.metadata(api)

    def api(self):
        return a.TaxAPI(MagicMock(DIVISION=3977752, BASE_URL='https://start.exactonline.nl', COLLECTIVE_DEBTOR_CODE='100100'))

    async def test_transport_rejects_all_writes_foreign_divisions_and_hosts(self):
        api = self.api()
        for method, url in [('POST', 'https://start.exactonline.nl/api/v1/3977752/financialtransaction/BankEntryLines'),
                            ('PUT', 'https://start.exactonline.nl/api/v1/3977752/financial/GLAccounts'),
                            ('GET', 'https://example.org/api/v1/3977752/financial/GLAccounts'),
                            ('GET', 'https://start.exactonline.nl/api/v1/1/financial/GLAccounts'),
                            ('GET', 'https://start.exactonline.nl/api/v1/3977752/salesentry/SalesEntries')]:
            with self.assertRaises(a.transport.Stop):
                await api.request(method, url)

    async def test_pagination_follows_every_page_and_rejects_foreign_next(self):
        root = 'https://start.exactonline.nl/api/v1/3977752/financial/GLAccounts'
        api = self.api()
        with patch.object(a.transport.Exact, 'request', AsyncMock(side_effect=[
            {'d': {'results': [{'Code': '1'}], '__next': root + '?skiptoken=next'}},
            {'d': {'results': [{'Code': '2'}]}}])) as get:
            self.assertEqual(await api.rows('financial/GLAccounts'), [{'Code': '1'}, {'Code': '2'}])
            self.assertEqual(get.await_count, 2)
        with patch.object(a.transport.Exact, 'request', AsyncMock(return_value={
            'd': {'results': [], '__next': 'https://example.org/api/v1/3977752/financial/GLAccounts'}})):
            with self.assertRaises(a.transport.Stop):
                await api.rows('financial/GLAccounts')

    async def test_budget_reserve_blocks_before_request(self):
        api = self.api(); api.limits = {'remaining': 150}
        with patch.object(a.transport.Exact, 'request', AsyncMock()) as get:
            with self.assertRaises(a.BudgetDeferred):
                await api.rows('financial/GLAccounts')
            get.assert_not_called()

    async def test_main_classifies_tax_before_outgoing_skip_or_order_lookup(self):
        from app import main
        with patch.object(main, 'bank_lines_on_suspense', AsyncMock(return_value=[bank(), bank(amount='100')])), \
             patch.object(main, 'find_receivable', AsyncMock()) as receivables:
            result = await main.bank_first_candidates()
            self.assertEqual([x['status'] for x in result['items']], ['TAX_IDENTIFIED', 'REVIEW_TAX'])
            self.assertTrue(all(x['order_number'] is None for x in result['items']))
            receivables.assert_not_called()

    async def test_financial_report_requires_existing_operator_session(self):
        from starlette.requests import Request
        request = Request({'type': 'http', 'session': {}})
        with self.assertRaises(a.HTTPException) as error:
            await a.report(request)
        self.assertEqual(error.exception.status_code, 401)

    def test_selection_has_no_top_limit_and_preserves_overlap(self):
        params = a.selection('00000000-0000-0000-0000-000000000001', datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc))
        self.assertNotIn('$top', params)
        self.assertIn("Modified ge datetime'2026-10-04T05:55:00'", params['$filter'])


if __name__ == '__main__':
    unittest.main()
