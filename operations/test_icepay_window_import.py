import unittest,hashlib,copy
from unittest.mock import patch
from decimal import Decimal
import xml.etree.ElementTree as ET
from operations import icepay_window_import as w
TEMPLATE=b'''<eExact><GLTransactions><GLTransaction entry="26260008">
<TransactionType number="20"/><Journal code="26"/><Date>2026-09-22</Date>
<FinYear number="2026"/><FinPeriod number="9"/><Description>old batch</Description>
<GLTransactionLine line="1" linetype="0" offsetline="0" status="20" type="40">
<Date>2026-09-22</Date><VATType>S</VATType><FinYear number="2026"/><FinPeriod number="9"/>
<GLAccount code="1100"/><Description>old payment</Description><Account code="100100"/>
<Amount><Currency code="EUR"/><Value>1.00</Value></Amount>
<References><PaymentReference>OLD12345</PaymentReference><YourRef>TD11111</YourRef></References>
<Note>old note</Note></GLTransactionLine></GLTransaction></GLTransactions></eExact>'''

def rows():
 result=[]
 for day,(count,total,entry) in w.DAY_TOTALS.items():
  for i in range(count):
   result.append(dict(payment_id=str(98000000+len(result)),merchant='34950',status='OK',order=str(70000+len(result)),date=day,amount=str(Decimal('1.00') if i<count-1 else total-(count-1))))
 return result

def build(source=None,template=TEMPLATE):
 with patch.object(w,'TEMPLATE_SHA',hashlib.sha256(template).hexdigest()):return w.build_xml(source or rows(),template)

def ledger(manifest):
 result=[]
 for i,r in enumerate(manifest):
  base=dict(ID='row'+str(i),EntryID='entry'+str(r['entry']),EntryNumber=r['entry'],Date=r['date'],Description=r['description'],Currency='EUR',JournalCode='27',YourRef=r['ref'],PaymentReference=r['payment_id'])
  result += [{**base,'GLAccountCode':'1317','AmountDC':r['amount'],'Account':None,'AccountCode':'','YourRef':None},{**base,'GLAccountCode':'1100','AmountDC':str(-Decimal(r['amount'])),'Account':w.DEBTOR,'AccountCode':'109419'}]
 return result

class WindowImport(unittest.TestCase):
 def test_manifest_is_only_authorized_receipts_with_own_references(self):
  xml,m=build();root=ET.fromstring(xml)
  self.assertEqual([e.get('entry') for e in root.findall('./GLTransactions/GLTransaction')],['26270004','26270005'])
  self.assertEqual(sum(Decimal(r['amount']) for r in m),Decimal('3027.51'))
  self.assertEqual(len(m),25)
  for line in root.findall('./GLTransactions/GLTransaction/GLTransactionLine'):
   r=next(r for r in m if r['payment_id']==line.findtext('References/PaymentReference'))
   self.assertEqual(line.findtext('References/YourRef'),r['ref'])
   self.assertEqual(line.find('Account').get('code'),'109419')
   self.assertEqual(line.find('GLAccount').get('code'),'1100')
   self.assertEqual(line.findtext('Amount/Value'),r['amount'])
  self.assertNotIn(b'OLD12345',xml)
 def test_bad_source_never_builds(self):
  for key,value in [('status','ERR'),('merchant','wrong'),('date','2026-10-06'),('amount','0'),('amount','1.01'),('order',None)]:
   r=rows();r[0][key]=value
   with self.assertRaises(ValueError):build(r)
  r=rows();r[1]['payment_id']=r[0]['payment_id']
  with self.assertRaises(ValueError):build(r)
 def test_changed_template_rejected(self):
  with self.assertRaises(ValueError):w.build_xml(rows(),TEMPLATE)
  with self.assertRaises(ValueError):build(template=TEMPLATE.replace(b'<Note>',b'<Unexpected/><Note>'))
 def test_empty_bank_is_ready(self):
  _,m=build();self.assertTrue(w.reconcile(m,[])['safe_to_import'])
 def test_existing_complete_batch_is_never_safe_to_upload(self):
  _,m=build();r=w.reconcile(m,ledger(m))
  self.assertTrue(r['complete']);self.assertFalse(r['safe_to_import'])
 def test_partial_or_modified_batch_blocks_upload(self):
  _,m=build()
  for actual in [ledger(m)[:2],ledger(m)[:-1]]:
   r=w.reconcile(m,actual);self.assertFalse(r['complete']);self.assertFalse(r['safe_to_import'])
  actual=ledger(m);actual[1]['YourRef']='TD99999'
  self.assertFalse(w.reconcile(m,actual)['complete'])
 def test_other_receipt_for_same_order_and_occupied_entry_block(self):
  _,m=build();actual=ledger(m)[:1];actual[0]['PaymentReference']='other';actual[0]['Description']='Prior receipt '+m[0]['ref'];actual[0]['EntryNumber']=26270001
  self.assertFalse(w.reconcile(m,actual)['safe_to_import'])
  self.assertEqual(w.reconcile(m,actual)['other_receipts_same_order'],[m[0]['payment_id']])

if __name__=='__main__':unittest.main()
