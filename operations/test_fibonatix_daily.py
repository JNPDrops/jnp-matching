import hashlib
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from unittest.mock import patch
from operations import fibonatix_daily as d


class DailyImportTests(unittest.TestCase):
    def receipt(self):
        return dict(payment_id='TEST0001',woo_id=123,ref='TD901',date='2026-10-06',amount='42.50',order_status='completed',source_status='Approved',source_kind='SL',source_status_code='20000')

    def lines(self):
        r=self.receipt();common=dict(EntryID='entry',EntryNumber=26260023,JournalCode='26',Currency='EUR',Date=r['date'],Description=d.description(r),PaymentReference=r['payment_id'],YourRef=r['ref'])
        return [dict(common,ID='bank',GLAccountCode='1316',AmountDC=42.50),dict(common,ID='debt',GLAccountCode='1100',AmountDC=-42.50,Account=d.DEBTOR,AccountCode='100100')]

    def test_reconcile_exact_receipt(self):
        got=d.reconcile([self.receipt()],self.lines());self.assertEqual(got['existing'],['TEST0001']);self.assertFalse(got['errors'])

    def test_wrong_debtor_not_deduplicated(self):
        lines=self.lines();lines[1]['AccountCode']='109419'
        self.assertEqual(d.reconcile([self.receipt()],lines)['errors'],['TEST0001'])

    def test_duplicate_receipt_fails(self):
        self.assertEqual(d.reconcile([self.receipt()],self.lines()*2)['errors'],['TEST0001'])

    def test_other_payment_for_order_is_flagged(self):
        lines=self.lines();extra=dict(lines[0],ID='other',Description='Fibonatix TD901 | Betaling OTHER001',PaymentReference='OTHER001');lines.append(extra)
        self.assertEqual(d.reconcile([self.receipt()],lines)['other_receipts_same_order'],['TEST0001'])

    def test_missing_receipt_is_not_existing(self):
        self.assertEqual(d.reconcile([self.receipt()],[])['missing'],['TEST0001'])

    def test_refund_uses_existing_clearing_without_debtor(self):
        refund=dict(self.receipt(),payment_id='REFUND01',source_kind='RF',amount='-42.50')
        lines=self.lines()
        for l in lines:
            l.update(PaymentReference='REFUND01',Description=d.description(refund),AmountDC=-l['AmountDC'])
        lines[1].update(GLAccountCode='1350',Account=None,AccountCode=None)
        self.assertEqual(d.reconcile([refund],lines+self.lines())['existing'],['REFUND01'])
        self.assertFalse(d.reconcile([refund],lines+self.lines())['other_receipts_same_order'])
        lines[1]['Account']=d.DEBTOR
        self.assertEqual(d.reconcile([refund],lines)['errors'],['REFUND01'])

    def test_refund_requires_own_original_and_amount_limit(self):
        refund=dict(self.receipt(),payment_id='REFUND01',source_kind='RF',amount='-42.50')
        d.validate_refund_origins([refund],self.lines())
        with self.assertRaisesRegex(ValueError,'refund_original_missing_or_ambiguous'):
            d.validate_refund_origins([dict(refund,ref='TD902')],self.lines())
        with self.assertRaisesRegex(ValueError,'refund_exceeds_original_receipt'):
            d.validate_refund_origins([dict(refund,amount='-42.51')],self.lines())
        with self.assertRaisesRegex(ValueError,'refund_original_debtor_unverified'):
            d.validate_refund_origins([refund],self.lines()[:1])

    def test_xml_uses_day_and_verified_order_identity(self):
        template=b'<eExact><GLTransactions><GLTransaction><TransactionType>20</TransactionType><Journal code="26"/><Date>2026-09-01</Date><FinYear number="2026"/><FinPeriod number="9"/><Description>template</Description><GLTransactionLine><Date>2026-09-01</Date><VATType>S</VATType><FinYear number="2026"/><FinPeriod number="9"/><GLAccount code="1100"/><Description>x</Description><Account code="100100"/><Amount><Currency code="EUR"/><Value>1</Value></Amount><References><PaymentReference>x</PaymentReference><YourRef>x</YourRef></References><Note>x</Note></GLTransactionLine></GLTransaction></GLTransactions></eExact>'
        with patch.object(d,'TEMPLATE_SHA',hashlib.sha256(template).hexdigest()):
            xml=d.build_xml([self.receipt()],template,date(2026,10,6),26260023)
            root=ET.fromstring(xml);line=root.find('.//GLTransactionLine')
            self.assertEqual(line.findtext('References/YourRef'),'TD901');self.assertEqual(line.findtext('References/PaymentReference'),'TEST0001')
            self.assertEqual(line.find('FinPeriod').get('number'),'10');self.assertEqual(line.find('Account').get('code'),'100100')
            for status in ['processing','pending','on-hold','cancelled','refunded','failed']:
                paid=self.receipt();paid['order_status']=status
                self.assertTrue(d.build_xml([paid],template,date(2026,10,6),26260023))
            refund=dict(self.receipt(),source_kind='RF',amount='-42.50')
            refund_line=ET.fromstring(d.build_xml([refund],template,date(2026,10,6),26260024)).find('.//GLTransactionLine')
            self.assertEqual(refund_line.find('GLAccount').get('code'),'1350')
            self.assertIsNone(refund_line.find('Account'))
            self.assertEqual(refund_line.findtext('Amount/Value'),'-42.50')
            bad=self.receipt();bad['source_status']='Declined'
            with self.assertRaisesRegex(ValueError,'invalid_receipt'):d.build_xml([bad],template,date(2026,10,6),26260023)


if __name__=='__main__':unittest.main()
