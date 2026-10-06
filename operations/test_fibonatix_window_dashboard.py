import unittest
from operations.fibonatix_window_dashboard import project

def data(state='exception'):
 r={'woo_id':101,'ref':'TD201','source_order':'TD201','entry':26260020,'trx':'TEST001','amount':'10.00','state':state,'exception':'invoice_missing_webshop_processing','invoice_candidates':[]}
 return {'prepared_at':'2026-10-06T09:00:00Z','receipts':[r]},[{'order_id':101,'order_number':'201','status':'processing'}]
class Dashboard(unittest.TestCase):
 def test_missing_invoice_keeps_actual_webshop_evidence_and_unknown_remaining(self):
  p,o=data();out=project(p,o,'2026-10-06T08:00:00Z',[]);r=out['receipts'][0]
  self.assertEqual(r['exception'],'invoice_missing');self.assertEqual(r['webshop_order']['status'],'processing');self.assertEqual(out['final_readback']['receivables'],[]);self.assertNotIn('webshop_order',p['receipts'][0])
 def test_no_completion_claim_for_pending_or_uncertain(self):
  for state in ['pending','match_requested']:
   p,o=data(state)
   with self.assertRaisesRegex(ValueError,'run_not_finished'):project(p,o,'2026-10-06T08:00:00Z',[])
 def test_wrong_order_mapping_rejected(self):
  p,o=data();o[0]['order_number']='202'
  with self.assertRaisesRegex(ValueError,'order_identity'):project(p,o,'2026-10-06T08:00:00Z',[])
if __name__=='__main__':unittest.main()
