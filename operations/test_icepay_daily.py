import hashlib
import unittest
from unittest.mock import patch
from datetime import date
import xml.etree.ElementTree as ET
from operations import icepay_daily as d

TEMPLATE=b'<eExact><GLTransactions><GLTransaction><TransactionType>20</TransactionType><Journal code="26"/><Date>2026-09-01</Date><FinYear number="2026"/><FinPeriod number="9"/><Description>x</Description><GLTransactionLine><Date>2026-09-01</Date><VATType>S</VATType><FinYear number="2026"/><FinPeriod number="9"/><GLAccount code="1100"/><Description>x</Description><Account code="100100"/><Amount><Currency code="EUR"/><Value>1</Value></Amount><References><PaymentReference>x</PaymentReference><YourRef>x</YourRef></References><Note>x</Note></GLTransactionLine></GLTransaction></GLTransactions></eExact>'

class DailyTests(unittest.TestCase):
    def receipt(self,day='2026-10-07'):
        return dict(payment_id='1234567',order='90001',date=day,status='OK',merchant='34950',amount='42.50',kind='receipt')
    def build(self,rows,day=date(2026,10,7),entry=26270007):
        with patch.object(d,'TEMPLATE_SHA',hashlib.sha256(TEMPLATE).hexdigest()):
            return d.build_xml(rows,TEMPLATE,day,entry)
    def ledger(self,manifest):
        r=manifest[0]; common=dict(ID='a',EntryID='entry',EntryNumber=r['entry'],JournalCode='27',Currency='EUR',Date=r['date'],Description=r['description'],PaymentReference=r['payment_id'],YourRef=r['ref'])
        return [dict(common,GLAccountCode='1317',AmountDC=42.50),dict(common,ID='b',GLAccountCode='1100',AmountDC=-42.50,AccountCode='109419',Account=d.DEBTOR)]
    def test_separate_calendar_days_year_and_period(self):
        for day,entry in [(date(2026,10,7),26270007),(date(2027,1,1),27270001)]:
            xml,_=self.build([self.receipt(day.isoformat())],day,entry);root=ET.fromstring(xml)
            self.assertEqual(root.find('.//Date').text,day.isoformat());self.assertEqual(root.find('.//FinYear').get('number'),str(day.year));self.assertEqual(root.find('.//FinPeriod').get('number'),str(day.month))
            self.assertEqual(root.find('.//GLTransaction').get('entry'),str(entry))
    def test_wrong_day_merchant_duplicate_and_failure_rejected(self):
        for rows in [[self.receipt('2026-10-06')],[dict(self.receipt(),merchant='OTHER')],[self.receipt(),self.receipt()],[dict(self.receipt(),status='ERR')]]:
            with self.assertRaises(ValueError):self.build(rows)
    def test_fee_has_no_debtor(self):
        row=dict(self.receipt(),kind='fee',order=None,amount='-1.00',cost_reference='1234567Costs',cost_description='verified fee')
        xml,_=self.build([row]);line=ET.fromstring(xml).find('.//GLTransactionLine')
        self.assertIsNone(line.find('Account'));self.assertEqual(line.find('GLAccount').get('code'),'5570')
    def test_existing_import_preserved(self):
        _,manifest=self.build([self.receipt()]);ledger=self.ledger(manifest)
        for l in ledger:l['EntryNumber']=26270006
        adapted=d.manifest_for_existing(manifest,ledger)
        self.assertEqual(adapted[0]['entry'],26270006);self.assertTrue(d.reconcile(adapted,ledger)['complete'])
    def test_wrong_account_and_double_receipt_block(self):
        _,manifest=self.build([self.receipt()]);ledger=self.ledger(manifest)
        ledger[1]['Account']='wrong'
        self.assertEqual(d.reconcile(manifest,ledger)['errors'],['1234567'])
        ledger=self.ledger(manifest)+[dict(self.ledger(manifest)[0],ID='c',PaymentReference='7654321',Description='ICEPAY TD90001 | Payment 7654321')]
        self.assertTrue(d.reconcile(manifest,ledger)['other_receipts_same_order'])
    def test_conflicting_payment_id_never_relabelled(self):
        _,manifest=self.build([self.receipt()]);ledger=self.ledger(manifest);ledger[1]['EntryNumber']=26270008
        with self.assertRaisesRegex(ValueError,'multiple_entries'):d.manifest_for_existing(manifest,ledger)
    def test_generic_identity_and_closed_day_required(self):
        good=dict(action='daily_prepare',params={'date':'2026-10-07'},task_key='jnp:3977752:icepay:2026-10-07')
        self.assertEqual(d.validate(good),date(2026,10,7))
        with self.assertRaises(ValueError):d.validate(dict(good,task_key='icepay-process-20261006-v1'))
        with self.assertRaises(ValueError):d.validate(dict(good,params={'date':'2099-01-01'}))
if __name__=='__main__':unittest.main()
