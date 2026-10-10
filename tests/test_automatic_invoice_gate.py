import copy
import unittest
from unittest.mock import AsyncMock
from operations.automatic_invoice_gate import assess, verify, partition, InvoiceNotReady

DEBTOR='00000000-0000-0000-0000-000000000001'


class InvoiceGateTests(unittest.TestCase):
    def setUp(self):
        self.receipts=[{'ref':'TD12345','payment_id':'synthetic-payment','amount':'100.00'}]
        self.sales=[dict(EntryID='synthetic-invoice',EntryNumber=12345,YourRef='TD12345',
                        Customer=DEBTOR,Currency='EUR',AmountFC=100,Type=20,Reversal=False)]
        self.opened=[dict(AccountId=DEBTOR,EntryNumber=12345,YourRef='TD12345',Amount=100)]

    def test_current_own_invoice_is_proven(self):
        self.assertEqual(assess(self.receipts,self.sales,self.opened,DEBTOR)[0]['entry_number'],12345)

    def test_different_debtor_blocks_even_when_order_and_amount_agree(self):
        self.sales[0]['Customer']='another-debtor'
        with self.assertRaisesRegex(InvoiceNotReady,'wrong_debtor'):
            assess(self.receipts,self.sales,self.opened,DEBTOR)

    def test_closed_missing_duplicate_credit_and_currency_are_not_ready(self):
        cases=[([],self.opened),(self.sales,[]),(self.sales*2,self.opened),
               ([self.sales[0]|{'Type':21}],self.opened),
               ([self.sales[0]|{'Currency':'USD'}],self.opened),
               (self.sales,[self.opened[0]|{'EntryNumber':99999}]),
               (self.sales,[self.opened[0]|{'AccountId':'other'}])]
        for sales,opened in cases:
            with self.subTest(sales=sales,opened=opened),self.assertRaises(InvoiceNotReady):
                assess(self.receipts,sales,opened,DEBTOR)

    def test_difference_limit_uses_current_remaining_in_both_directions(self):
        for paid in ('99.00','100.00','101.00'):
            assess([self.receipts[0]|{'amount':paid}],self.sales,self.opened,DEBTOR)
        for paid in ('98.99','101.01'):
            with self.assertRaisesRegex(InvoiceNotReady,'above_one_euro'):
                assess([self.receipts[0]|{'amount':paid}],self.sales,self.opened,DEBTOR)
        # Original total cannot conceal a substantial remaining difference.
        with self.assertRaisesRegex(InvoiceNotReady,'above_one_euro'):
            assess(self.receipts,self.sales,[self.opened[0]|{'Amount':50}],DEBTOR)

    def test_one_order_cannot_split_difference_over_two_receipts(self):
        duplicate=self.receipts+[self.receipts[0]|{'payment_id':'another-payment'}]
        with self.assertRaisesRegex(InvoiceNotReady,'multiple_selected'):
            assess(duplicate,self.sales,self.opened,DEBTOR)

    def test_bad_order_does_not_remove_an_independent_good_order(self):
        receipts=self.receipts+[dict(ref='TD12346',payment_id='bad-payment',amount='100.00')]
        sales=self.sales+[self.sales[0]|{'YourRef':'TD12346','EntryNumber':12346,'Customer':'wrong-debtor'}]
        eligible,exceptions=partition(receipts,sales,self.opened,DEBTOR)
        self.assertEqual([r['receipt']['payment_id'] for r in eligible],['synthetic-payment'])
        self.assertEqual(exceptions,[{'reference':'TD12346','payment_id':'bad-payment','reason':'own_sale_on_wrong_debtor'}])

    def test_multiple_receipts_and_refunds_stay_scoped_exceptions(self):
        for receipt in (self.receipts[0]|{'refund_review':True},):
            eligible,exceptions=partition([receipt],self.sales,self.opened,DEBTOR)
            self.assertEqual(eligible,[])
            self.assertEqual(exceptions[0]['reason'],'own_order_refund_requires_review')
        eligible,exceptions=partition(self.receipts+[self.receipts[0]|{'payment_id':'second'}],self.sales,self.opened,DEBTOR)
        self.assertEqual(eligible,[])
        self.assertEqual(len(exceptions),2)


class ReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_does_not_hide_sales_on_other_debtors(self):
        api=AsyncMock()
        api.rows.side_effect=[
            [dict(ID=DEBTOR,Code='109419',IsSales=True,Status='C')],
            [dict(EntryID='invoice',EntryNumber=12345,YourRef='TD12345',Customer='other',
                  Currency='EUR',AmountFC=100,Type=20,Reversal=False)],
            [dict(AccountId='other',EntryNumber=12345,YourRef='TD12345',Amount=100)]]
        with self.assertRaisesRegex(InvoiceNotReady,'wrong_debtor'):
            await verify(api,[{'order':'TD12345','payment_id':'id','amount':'100'}],'109419')
        for call in api.rows.await_args_list[1:]:
            self.assertNotIn('Customer',call.args[1]['$filter'])
            self.assertNotIn('AccountId',call.args[1]['$filter'])


if __name__=='__main__':unittest.main()
