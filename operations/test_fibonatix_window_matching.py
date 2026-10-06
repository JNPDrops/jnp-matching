import copy,unittest
from operations import fibonatix_window_matching as w

def fixture():
 r={'ref':'TD90001','source_order':'TD90001','trx':'SYNTH001','amount':'10.00','description':'own receipt','order_status':'completed'}
 inv={'YourRef':r['ref'],'JournalCode':'70','GLAccountCode':'1100','AmountDC':10,'Account':w.DEBTOR,'AccountCode':'100100','Currency':'EUR','EntryNumber':26799901}
 base={'AccountId':w.DEBTOR,'AccountCode':'100100','CurrencyCode':'EUR','AmountInTransit':0,'YourRef':r['ref']}
 opened=[{**base,'JournalCode':'70','InvoiceNumber':26799901,'Amount':10},{**base,'JournalCode':'26','Amount':-10,'Description':r['description']+' SYNTH001'}]
 r['description']=opened[1]['Description'];return r,[inv],opened
class Matching(unittest.TestCase):
 def test_only_own_equal_open_invoice(self):
  r,h,o=fixture();self.assertIsNone(w.classify(r,h,o))
 def test_missing_invoice_keeps_processing_evidence(self):
  r,h,o=fixture();r['order_status']='processing';self.assertEqual(w.classify(r,[],o),'invoice_missing_webshop_processing')
 def test_equal_amount_different_order_is_no_match(self):
  r,h,o=fixture();h[0]['YourRef']='TD90002';self.assertEqual(w.classify(r,h,o),'invoice_missing_webshop_completed')
 def test_partial_other_debtor_and_credit_hold(self):
  for key,value,reason in [('Amount',9,'partial_amount_or_in_transit'),('AmountInTransit',1,'partial_amount_or_in_transit'),('AccountId','other','open_item_identity_changed')]:
   r,h,o=fixture();o[0][key]=value;self.assertEqual(w.classify(r,h,o),reason)
  r,h,o=fixture();self.assertEqual(w.classify(r,h,o[:1]),'invoice_closed_or_receipt_not_fully_open')
 def test_cent_difference_and_cancelled_not_automatically_settled(self):
  r,h,o=fixture();h[0]['AmountDC']=10.01;self.assertEqual(w.classify(r,h,o),'amount_difference_requires_case_decision')
  r,h,o=fixture();r['order_status']='cancelled';self.assertEqual(w.classify(r,h,o),'cancelled_order_requires_review')
 def test_duplicate_invoice_holds(self):
  r,h,o=fixture();self.assertEqual(w.classify(r,h+h,o),'multiple_invoices_for_order')
 def test_unknown_save_blocks_later_groups(self):
  manifest=[{'trx':str(i),'amount':'1.00','ref':'TD'+str(i),'description':'test','entry':26260020,'woo_id':i} for i in range(248)]
  receipts=[{**r,'source_order':r['ref'],'bank_line_id':str(i),'state':'pending'} for i,r in enumerate(manifest)]
  p={'manifest':manifest,'receipts':receipts};imp={'summary':{'state':'import_verified','verified_receipts':248},'artifacts':{'manifest':manifest}}
  w.approved(p,imp);p['receipts'][0]['state']='match_requested'
  with self.assertRaisesRegex(ValueError,'unknown_save'):w.approved(p,imp)
if __name__=='__main__':unittest.main()
