import copy
import sys
import types
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from operations.fibonatix_daily_automatic import checked, refunded_ids, refund_selection_start, BANK


class AutomaticScopeTests(unittest.TestCase):
    def setUp(self):
        self.first=date(2026,10,3);self.last=date(2026,10,6)
        self.allowed={'TEST0001':{'date':'2026-10-06','ref':'TD901','woo_id':123,'amount':'42.50'}}
        self.snapshot={'controls':[{'id':'BankAccount','value':BANK},{'id':'Notes','value':'Fibonatix TD'},
                       {'id':'Status1','checked':True},{'id':'Status2','checked':False},
                       {'id':'EntryDate_Selection','value':'1000'}, {'id':'EntryDate_From','value':'03-10-2026'}, {'id':'EntryDate_To','value':'06-10-2026'}],
                       'rows':[{'cells':['','06-10-2026','','EUR','42.50','0','','1100 - Debiteuren','100100 - Fibo','','EUR'],
                                'note':'Fibonatix TD901 | Woo 123 | Betaling TEST0001'}]}

    def run_check(self):
        mods={'operations.fibonatix_import':types.SimpleNamespace(statement_rows=lambda s:s['rows']),
              'operations.strict_order_matching':types.SimpleNamespace(euro=lambda v:Decimal(v))}
        with patch.dict(sys.modules,mods):return checked(self.snapshot,self.allowed,self.first,self.last)

    def test_exact_scope_passes(self):self.assertEqual(self.run_check()[0]['payment_id'],'TEST0001')
    def test_unknown_receipt_blocks(self):
        self.allowed={}
        with self.assertRaisesRegex(ValueError,'receipt_not_authorized'):self.run_check()
    def test_wrong_bank_blocks(self):
        self.snapshot['controls'][0]['value']='other'
        with self.assertRaisesRegex(ValueError,'wrong_bank'):self.run_check()
    def test_future_receipt_blocks(self):
        self.snapshot['rows'][0]['cells'][1]='07-10-2026'
        with self.assertRaisesRegex(ValueError,'receipt_evidence_mismatch'):self.run_check()
    def test_wrong_customer_blocks(self):
        self.snapshot['rows'][0]['cells'][8]='109419 - ICEPAY'
        with self.assertRaisesRegex(ValueError,'wrong_receipt_account'):self.run_check()
    def test_duplicate_blocks(self):
        self.snapshot['rows']*=2
        with self.assertRaisesRegex(ValueError,'duplicate_receipt'):self.run_check()

    def test_refund_excludes_only_its_own_order(self):
        allowed={'A':{'woo_id':123},'B':{'woo_id':456}}
        refund={'Type':'RF','Status(approved/declined)':'Approved','Status Code':'20000','Currency':'EUR','Brand TRX ID':'123','Amount':'42.50'}
        self.assertEqual(refunded_ids(allowed,[refund]),{'A'})
        refund['Status(approved/declined)']='Pending'
        self.assertEqual(refunded_ids(allowed,[refund]),set())

    def test_refund_cannot_remain_inside_native_selection(self):
        eligible=[{'date':'2026-10-06'}]
        self.assertEqual(refund_selection_start(eligible,[{'date':'2026-10-03'}]),date(2026,10,6))
        with self.assertRaisesRegex(ValueError,'refund_requires_narrower_selection'):
            refund_selection_start(eligible,[{'date':'2026-10-06'}])


if __name__=='__main__':unittest.main()
