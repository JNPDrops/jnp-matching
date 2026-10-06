import copy
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch
from operations import bank_daily_automatic as b


class BankAutomaticTests(unittest.TestCase):
    def setUp(self):
        self.modules=patch.dict(sys.modules,{'operations.fibonatix_import':types.SimpleNamespace(statement_rows=lambda s:s['rows']), 'operations.strict_order_matching':types.SimpleNamespace(euro=lambda v:Decimal(v.replace(',','.')) if v else Decimal(0))})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def target(self):
        return dict(status='bacs_match_candidate',journal_code='20',account_code='109372',currency='EUR',payment_method='bacs',order_status='completed',reference='TD900',invoice_reference='TD900',amount='90.99',bank_line_id='line',bank_entry_id='entry',invoice_entry='invoice',description='Synthetic bank receipt TD900',bank_date='2026-09-18')

    def snapshot(self):
        target=self.target()
        controls=[dict(id='BankAccount',value=b.BANK),dict(id='Notes',value=target['description']),dict(id='Status1',checked=True),dict(id='Status2',checked=False),dict(id='EntryDate_Selection',value='1000'),dict(id='EntryDate_From',value='18-09-2026'),dict(id='EntryDate_To',value='18-09-2026')]
        cells=['','18-09-2026','','EUR','90,99','','','1100 - Debiteuren','109372 - BACS','','EUR','','','','','']
        return dict(controls=controls,rows=[dict(cells=cells,note=target['description'])])

    def test_exact_native_selection(self):
        b.validate_target(self.target())
        self.assertEqual(len(b.checked(self.snapshot(),self.target())),1)

    def test_wrong_bank_amount_account_or_date_rejected(self):
        for field,value in [(4,'91,99'),(8,'100100 - Other'),(1,'19-09-2026')]:
            snap=self.snapshot();snap['rows'][0]['cells'][field]=value
            with self.assertRaises(ValueError):b.checked(snap,self.target())
        snap=self.snapshot();snap['controls'][0]['value']='another-bank'
        with self.assertRaises(ValueError):b.checked(snap,self.target())

    def test_duplicate_selection_rejected(self):
        snap=self.snapshot();snap['rows']+=copy.deepcopy(snap['rows'])
        with self.assertRaises(ValueError):b.checked(snap,self.target())

    def test_unproved_or_non_bacs_target_rejected(self):
        for field,value in [('payment_method','icepay'),('order_status','processing'),('invoice_reference','TD901'),('invoice_entry',None)]:
            target=self.target();target[field]=value
            with self.assertRaises(ValueError):b.validate_target(target)


if __name__=='__main__':unittest.main()
