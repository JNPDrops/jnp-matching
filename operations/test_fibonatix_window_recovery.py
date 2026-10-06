import unittest
from operations.fibonatix_window_recovery import resolve
from operations.test_fibonatix_window_import import build,ledger
from operations.fibonatix_window_matching import DEBTOR
class Recovery(unittest.TestCase):
 def setup_source(self):
  _,m=build();r=m[0];r.update(source_order=r['ref'],invoice={'EntryNumber':26799999});return r,ledger([r])
 def test_confirmed_saved_identity_and_closed_api_required(self):
  r,l=self.setup_source();seen=[]
  self.assertEqual(resolve(r,[{'checked':True}],l,[],lambda *a,**k:seen.append(True)),'matched_verified');self.assertEqual(seen,[True])
  with self.assertRaises(ValueError):resolve(r,[{'checked':True}],l[:1],[],lambda *a,**k:None)
 def test_full_open_unselected_is_no_retry_exception(self):
  r,l=self.setup_source();base={'AccountId':DEBTOR,'AccountCode':'100100','CurrencyCode':'EUR','YourRef':r['ref'],'AmountInTransit':0}
  o=[{**base,'JournalCode':'70','InvoiceNumber':26799999,'Amount':r['amount']},{**base,'JournalCode':'26','Description':r['description'],'Amount':'-'+r['amount']}]
  self.assertEqual(resolve(r,[],l,o,lambda *a,**k:None),'not_applied_verified')
  with self.assertRaises(ValueError):resolve(r,[],l,[],lambda *a,**k:None)
  o[1]['AmountInTransit']=1
  with self.assertRaises(ValueError):resolve(r,[],l,o,lambda *a,**k:None)
if __name__=='__main__':unittest.main()
