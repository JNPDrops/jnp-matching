import unittest,hashlib,copy
from unittest.mock import patch
from decimal import Decimal
import xml.etree.ElementTree as ET
from operations import fibonatix_window_import as w
from operations.test_icepay_window_import import TEMPLATE

def source():
 result=[]
 for day,(count,total,entry) in w.DAYS.items():
  for i in range(count):
   n=len(result);result.append({'trx':'TEST'+str(100000+n),'payment_id':'TEST'+str(100000+n),'ref':'TD'+str(90000+n),'woo_id':500000+n,'date':day,'amount':str(Decimal('1.00') if i<count-1 else total-(count-1))})
 return result

def build(rows=None):
 with patch.object(w,'TEMPLATE_SHA',hashlib.sha256(TEMPLATE).hexdigest()):return w.build_xml(rows or source(),TEMPLATE)

def ledger(manifest):
 result=[]
 for i,r in enumerate(manifest):
  b=dict(EntryID='entry'+str(r['entry']),EntryNumber=r['entry'],Date=r['date'],Description=r['description'],Currency='EUR',JournalCode='26',YourRef=r['ref'],PaymentReference=r['trx'])
  result += [{**b,'ID':'bank'+str(i),'GLAccountCode':'1316','AmountDC':r['amount'],'Account':None,'AccountCode':'','YourRef':None},{**b,'ID':'credit'+str(i),'GLAccountCode':'1100','AmountDC':str(-Decimal(r['amount'])),'Account':w.DEBTOR,'AccountCode':'100100'}]
 return result
class Import(unittest.TestCase):
 def test_correct_entries_and_own_order_reference(self):
  xml,m=build();self.assertEqual(sum(Decimal(r['amount']) for r in m),Decimal('26879.74'));self.assertEqual(len(m),248)
  root=ET.fromstring(xml);self.assertEqual([e.get('entry') for e in root.findall('./GLTransactions/GLTransaction')],['26260020','26260021','26260022'])
  for r,l in zip(m,root.findall('./GLTransactions/GLTransaction/GLTransactionLine')):
   self.assertEqual(l.findtext('References/YourRef'),r['ref']);self.assertEqual(l.findtext('References/PaymentReference'),r['trx']);self.assertEqual(l.find('Account').get('code'),'100100');self.assertEqual(l.find('GLAccount').get('code'),'1100')
 def test_duplicate_order_source_and_outside_scope_rejected(self):
  for key,value in [('ref','bad'),('date','2026-10-06'),('amount','0'),('woo_id','500000')]:
   r=source();r[0][key]=value
   with self.assertRaises(ValueError):build(r)
  r=source();r[0]['trx']=r[1]['trx']
  with self.assertRaises(ValueError):build(r)
 def test_complete_and_empty(self):
  _,m=build();self.assertTrue(w.reconcile(m,[])['safe_to_import']);r=w.reconcile(m,ledger(m));self.assertTrue(r['complete']);self.assertFalse(r['safe_to_import'])
 def test_partial_and_corrected_financial_records_block_retry(self):
  _,m=build();actual=ledger(m)
  for bad in [actual[:-1],actual[:2]]:self.assertFalse(w.reconcile(m,bad)['safe_to_import']);self.assertFalse(w.reconcile(m,bad)['complete'])
  for key,value in [('YourRef','TD99999'),('Account','different'),('AmountDC','-5.00'),('Date','2026-10-06')]:
   bad=copy.deepcopy(actual);bad[1][key]=value;self.assertFalse(w.reconcile(m,bad)['complete'])
 def test_other_reference_same_order_blocks(self):
  _,m=build();a=ledger(m)[:1];a[0]['PaymentReference']='different';a[0]['Description']='Prior receipt '+m[0]['ref'];a[0]['EntryNumber']=26260019
  self.assertFalse(w.reconcile(m,a)['safe_to_import'])
if __name__=='__main__':unittest.main()
