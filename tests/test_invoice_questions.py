import copy
import unittest

from app.dashboard.invoice_questions import annotate
from app.dashboard.worklist import bank_case, finalize, strict_cases


class InvoiceQuestionTests(unittest.TestCase):
    def item(self, category='invoice_missing'):
        return {'category': category, 'reference': 'TD12345', 'status': 'open'}

    def test_purchase_missing_is_separate_from_uncertain_supplier_documents(self):
        self.assertEqual(annotate(self.item('supplier_invoice_missing'))['question_group'], 'purchase_missing')
        for category in ['supplier_review', 'supplier_invoice_review']:
            self.assertEqual(annotate(self.item(category))['question_group'], 'purchase_review')

    def test_processing_requires_same_order_and_absent_invoice(self):
        order = {'order_number':'#12345', 'order_id':123, 'status':'processing'}
        result = annotate(self.item(), order=order, invoice_absent=True)
        self.assertEqual(result['question_group'], 'sales_waiting')
        self.assertEqual(result['status'], 'open')  # presentation never decides or matches
        for kwargs in ({'invoice_absent':False}, {'invoice_absent':True,'expected_order_id':'124'}):
            self.assertEqual(annotate(self.item(), order=order, **kwargs)['question_group'], 'sales_review')
        self.assertEqual(annotate(self.item(), order={**order,'order_number':'#99999'},
                                  invoice_absent=True)['order_import']['state'], 'identity_mismatch')

    def test_hold_cancelled_and_completed_are_not_promised_future_imports(self):
        for status in ['pending','on-hold','completed','cancelled','failed','refunded']:
            result = annotate(self.item(), order={'order_number':'#12345','order_id':123,'status':status},
                              invoice_absent=True)
            self.assertEqual(result['question_group'], 'sales_review')
            self.assertNotEqual(result['order_import']['state'], 'awaiting_import')

    def test_unavailable_does_not_mean_not_found(self):
        self.assertEqual(annotate(self.item(),lookup_state='unavailable')['order_import']['state'], 'unavailable')
        self.assertEqual(annotate(self.item(),lookup_state='not_found')['order_import']['state'], 'not_found')

    def test_display_group_preserves_human_decision_fingerprint(self):
        raw = {'status':'invoice_missing','reference':'TD12345'}
        item = bank_case('bank-test',raw,'2026-10-05T20:00:00Z')
        before = copy.deepcopy(item)
        for key in ['question_group','question_group_label','order_import']:
            before.pop(key,None)
        self.assertEqual(list(finalize([item]))[0]['fingerprint'],list(finalize([before]))[0]['fingerprint'])

    def test_bank_projection_uses_stored_order_check_not_bare_status(self):
        raw = {'status':'invoice_missing','reference':'TD12345','order_status':'processing',
               'woo_order_id':123,'invoice_presence':'not_found'}
        self.assertEqual(bank_case('bank-test',raw,None)['question_group'],'sales_review')
        self.assertEqual(bank_case('bank-test',{**raw,'order_check':'observed'},None)['question_group'],'sales_waiting')

    def test_ambiguous_fibonatix_invoice_is_not_classified_waiting(self):
        receipt = {'source_order':'TD12345','woo':'123','trx':'TEST1234','amount':'10.00',
            'state':'exception_verified','exception':'invoice_missing_or_ambiguous',
            'webshop_order':{'order_number':'#12345','order_id':123,'status':'processing'},
            'webshop_lookup_state':'found','invoice_refresh':{'rows':[{'ID':'one'},{'ID':'two'}]}}
        plan = {'created_at':'2026-10-05T20:00:00Z','receipts':[receipt]}
        self.assertEqual(strict_cases('test',plan,None)[0]['question_group'],'sales_review')
        receipt['invoice_refresh']['rows']=[]
        self.assertEqual(strict_cases('test',plan,None)[0]['question_group'],'sales_waiting')


if __name__=='__main__':
    unittest.main()
