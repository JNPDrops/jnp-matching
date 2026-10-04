"""Synthetic acquisition/CSV regressions; no credentials, network or Exact."""
import csv
import io
import json
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from operations import icepay_transactions as t
from operations.test_icepay_browser import element, collection

HEADERS = ['PaymentID','MerchantID','PaymentTime','LastPaymentStatus','Amount',
           'Currency','Checkout Reference','Checkout Description','Description']
ROW = ['123','34950','10/01/2026 2:30:00 PM','OK','24,95','EUR','Order #49042','Order #49042','Order #49042']


def fixture(rows):
    output = io.StringIO()
    writer = csv.writer(output,delimiter=';')
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return output.getvalue().encode('utf-8-sig')


class CSV(unittest.TestCase):
    def test_us_csv_date_differs_from_day_month_portal_control(self):
        rows, summary = t.parse_payments(fixture([ROW]),1)
        self.assertEqual(rows[0]['date'],'2026-10-01')
        self.assertEqual(rows[0]['order'],'49042')
        self.assertEqual(summary['td_ok_total'],'24.95')

    def test_all_three_dates_and_other_merchants_are_reconciled(self):
        rows = []
        for index,day in enumerate(('01','02','03'),1):
            row = ROW.copy()
            row[0],row[2] = str(index),f'10/{day}/2026 1:00:00 AM'
            rows.append(row)
        other = ROW.copy()
        other[0],other[1] = '999','99999'
        rows.append(other)
        selected,summary = t.parse_payments(fixture(rows),4)
        self.assertEqual(len(selected),3)
        self.assertEqual(summary['other_merchant_rows'],1)
        self.assertEqual(summary['td_ok_total'],'74.85')
        self.assertEqual([x['count'] for x in summary['per_day'].values()],[1,1,1])

    def test_count_duplicate_currency_and_scope_fail_closed(self):
        bad_date = ROW.copy(); bad_date[2]='10/04/2026 1:00:00 AM'
        bad_currency = ROW.copy(); bad_currency[5]='USD'
        conflict = ROW.copy(); conflict[7]='Order #49043'
        for rows,count,reason in [([ROW],2,'count_mismatch'),([ROW,ROW],2,'duplicate_payment'),
                ([bad_date],1,'out_of_period'),([bad_currency],1,'unexpected_currency'),
                ([conflict],1,'order_reference_conflict')]:
            with self.assertRaises(t.AcquisitionStopped) as caught:
                t.parse_payments(fixture(rows),count)
            self.assertEqual(str(caught.exception),reason)

    def test_failed_and_unresolved_payments_are_not_silently_receipts(self):
        failed = ROW.copy(); failed[0]='124'; failed[3]='ERR'
        unresolved = ROW.copy(); unresolved[0]='125'; unresolved[6:]=['','','']
        _,summary = t.parse_payments(fixture([ROW,failed,unresolved]),3)
        self.assertEqual(summary['td_ok_count'],2)
        self.assertEqual(summary['non_ok_count'],1)
        self.assertEqual(summary['missing_order_count'],1)

    def test_csv_ids_must_match_every_observed_page(self):
        t.parse_payments(fixture([ROW]),1,['123'])
        with self.assertRaises(t.AcquisitionStopped) as caught:
            t.parse_payments(fixture([ROW]),1,['124'])
        self.assertEqual(str(caught.exception),'count_mismatch')

    def test_amounts_never_round_invalid_or_guess_grouping(self):
        for value in ('1,234.56','1.234,56','NaN','Infinity','1.234','EUR 4',''):
            with self.assertRaises(t.AcquisitionStopped):
                t.amount(value)
        self.assertEqual(str(t.amount('-24,95')),'-24.95')

    def test_decoder_rejects_unexpected_artifacts_and_raw_secret_errors(self):
        valid = {'status':t.status('downloaded','complete',account_verified=True),
                 'artifacts':{'proof':{'period':t.RANGE,'ui_payment_count':1}}}
        self.assertTrue(t.decode(json.dumps(valid).encode())[0]['account_verified'])
        valid['artifacts']['cookies']='private'
        with self.assertRaises(t.AcquisitionStopped):
            t.decode(json.dumps(valid).encode())
        self.assertEqual(str(t.AcquisitionStopped('private-password')),'runtime_error')


class Calendar(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_snapshot_waits_for_page_size_transition(self):
        old={'ids':['1'],'next':True,'next_count':1}
        new={'ids':['1','2'],'next':False,'next_count':0}
        page=SimpleNamespace(evaluate=AsyncMock(side_effect=[old,new,new,new]))
        with patch.object(t.asyncio,'sleep',AsyncMock()):
            self.assertEqual(await t.stable_payment_page(page,100),new)

    async def test_payment_pages_are_counted_by_ids_without_footer_or_row_text(self):
        state={'page':0}
        next_button=element()
        next_button.is_enabled.side_effect=lambda:state['page']==0
        next_button.get_attribute.return_value=None
        async def advance(): state['page']=1
        next_button.click.side_effect=advance
        page=SimpleNamespace(locator=lambda _:collection([]),
            get_by_role=lambda *_a,**_k:collection([next_button]),wait_for_load_state=AsyncMock())
        async def ids(_): return ['101','102'] if state['page']==0 else ['103']
        async def snapshot(_page,_size):
            return {'ids':await ids(_page),'next':state['page']==0,'next_count':1}
        with patch.object(t.b,'wait_verified_account',AsyncMock()), \
             patch.object(t.b,'guard_page',AsyncMock()), \
             patch.object(t,'stable_payment_page',side_effect=snapshot):
            self.assertEqual(await t.payment_identifiers(page),['101','102','103'])
        next_button.click.assert_awaited_once()

    async def test_date_field_waits_for_filter_drawer_visibility(self):
        field = element()
        locator = SimpleNamespace(wait_for=AsyncMock(),all=AsyncMock(return_value=[field]))
        page = SimpleNamespace(locator=lambda _:locator)
        self.assertIs(await t.date_field(page,'tableFiltersForm.PaymentTime.PaymentTime'),field)
        locator.wait_for.assert_awaited_once_with(state='visible',timeout=15000)

    async def test_day_selection_excludes_adjacent_month_and_disabled_cells(self):
        current, adjacent, disabled = element(),element(),element()
        current.evaluate.return_value = True
        adjacent.evaluate.return_value = False
        disabled.evaluate.return_value = False
        heading = element('October 2026')
        table = SimpleNamespace(is_visible=AsyncMock(return_value=True),
            locator=lambda _:heading,get_by_role=lambda *_a,**_k:collection([current,adjacent,disabled]))
        calendar = SimpleNamespace(locator=lambda _:collection([table]))
        await t.select_october_day(calendar,1)
        current.click.assert_awaited_once()
        adjacent.click.assert_not_awaited()
        disabled.click.assert_not_awaited()

    async def test_ambiguous_calendar_never_clicks(self):
        cells = [element(),element()]
        for cell in cells: cell.evaluate.return_value=True
        table = SimpleNamespace(is_visible=AsyncMock(return_value=True),
            locator=lambda _:element('October 2026'),get_by_role=lambda *_a,**_k:collection(cells))
        with self.assertRaises(t.AcquisitionStopped):
            await t.select_october_day(SimpleNamespace(locator=lambda _:collection([table])),1)
        for cell in cells: cell.click.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
