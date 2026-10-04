import unittest
from decimal import Decimal
from operations import icepay_apply as a
from operations.icepay_booking import build_xml
from operations.test_icepay_booking import receipts, TEMPLATE


def fixture():
    _,manifest,_=build_xml(receipts(),TEMPLATE)
    ledger=[]
    for source in manifest:
        for gl,sign in [('1317',1),('1100',-1)]:
            ledger.append({'ID':source['payment_id']+'-'+gl,'GLAccountCode':gl,
                'AmountDC':str(Decimal(source['amount'])*sign),'AccountCode':'109419' if gl=='1100' else '',
                'Account':'0492e907-6698-4281-98e5-c46e01ae9219' if gl=='1100' else None,
                'YourRef':source['ref'],'JournalCode':'27','EntryNumber':source['entry'],
                'FinancialYear':2026,'FinancialPeriod':10,'Date':source['date'],
                'Description':source['description'],'PaymentReference':source['payment_id']})
    return manifest,ledger


class Readback(unittest.TestCase):
    def test_exactly_one_balanced_bank_and_own_debtor_line_per_receipt(self):
        manifest,ledger=fixture(); result=a.reconcile(manifest,ledger)
        self.assertTrue(result['complete']); self.assertFalse(result['safe_to_import'])
        self.assertEqual(len(result['verified_ids']),36)

    def test_partial_existing_and_changed_own_order_never_allow_repeat_upload(self):
        manifest,ledger=fixture()
        for rows in (ledger[:1],ledger[1:],ledger+ledger[:1]):
            result=a.reconcile(manifest,rows)
            self.assertFalse(result['complete']); self.assertFalse(result['safe_to_import'])
        ledger[1]['YourRef']='TD99999'
        result=a.reconcile(manifest,ledger)
        self.assertEqual(len(result['errors']),1); self.assertFalse(result['complete'])

    def test_changed_sign_date_account_and_entry_fail_independent_readback(self):
        for key,value in [('AmountDC','1.00'),('Date','2026-09-30'),('AccountCode','100100'),('EntryNumber',26260001)]:
            manifest,ledger=fixture(); ledger[1][key]=value
            self.assertFalse(a.reconcile(manifest,ledger)['complete'])

    def test_only_empty_unoccupied_selection_is_safe_to_upload(self):
        manifest,_=fixture()
        self.assertTrue(a.reconcile(manifest,[])['safe_to_import'])
        occupied=[{'EntryNumber':26270001,'Description':'unrelated payment','PaymentReference':''}]
        self.assertFalse(a.reconcile(manifest,occupied)['safe_to_import'])


if __name__=='__main__': unittest.main()
