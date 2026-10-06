import copy,unittest
from operations.icepay_window_matching import approved_plan
from operations.test_icepay_window_import import build

def fixture():
 _,manifest=build()
 receipts=[{**r,'bank_line_id':str(i),'source_order':r['ref'],'debtor':'109419','journal':'27','currency':'EUR','state':'pending'} for i,r in enumerate(manifest)]
 return {'manifest':manifest,'receipts':receipts},({'manifest':manifest},{'state':'import_verified','verified_receipts':25})

class Approved(unittest.TestCase):
 def test_verified_own_source_plan_only(self):approved_plan(*fixture())
 def test_changed_order_amount_debtor_currency_block(self):
  for key,value in [('source_order','TD1'),('amount','0'),('debtor','100100'),('currency','USD'),('journal','26')]:
   p,i=fixture();p['receipts'][0][key]=value
   with self.assertRaises(ValueError):approved_plan(p,i)
 def test_unknown_save_halts_all_groups(self):
  p,i=fixture();p['receipts'][0]['state']='match_requested'
  with self.assertRaisesRegex(ValueError,'unknown_save'):approved_plan(p,i)
 def test_no_unverified_or_duplicate_bank_sources(self):
  p,i=fixture();i[1]['state']='upload_requested'
  with self.assertRaises(ValueError):approved_plan(p,i)
  p,i=fixture();p['receipts'][0]['bank_line_id']=p['receipts'][1]['bank_line_id']
  with self.assertRaises(ValueError):approved_plan(p,i)
if __name__=='__main__':unittest.main()
