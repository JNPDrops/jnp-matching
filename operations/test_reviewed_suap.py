import unittest
from unittest.mock import MagicMock

from operations import reviewed_suap as r, debtor_routing_policy as p
from operations import bacs_debtor_transfer as m


class ReviewedSuapTests(unittest.TestCase):
    def test_review_retains_corrected_credit_order_identities(self):
        self.assertEqual(len(r.ENTRIES),343)
        a=r.ENTRIES['104f41d9-2437-4ac2-9f46-196e9c805fec']
        b=r.ENTRIES['7799e83a-8a9d-4fdc-94fd-1a69f9b2a36f']
        self.assertEqual((a['reference'],a['order_reference'],a['order_id']),('TD113933','TD41300',111850))
        self.assertEqual((b['reference'],b['order_reference'],b['order_id']),('TD116518','TD40287',108478))
        selection={**a,'payment_method':r.METHOD,'work_scope':'cleanup'}
        r.validate_selection(selection)
        for change in ({'order_reference':'TD40287'},{'order_id':108478},
                       {'entry_type':20},{'payment_method':'np_payments'},
                       {'work_scope':'continuous'},{'entry_id':'00000000-0000-0000-0000-000000000001'}):
            with self.assertRaises(m.Stop):r.validate_selection({**selection,**change})

    def test_reconciliation_reads_queue_and_reports_missing_proof_without_mutations(self):
        conn=MagicMock()
        a=r.ENTRIES['104f41d9-2437-4ac2-9f46-196e9c805fec']
        conn.execute.return_value.fetchall.return_value=[(a['entry_id'],a['reference'],a['order_reference'],
            {'order_id':a['order_id'],'payment_method':r.METHOD},21,'pending','cleanup')]
        conn.execute.return_value.fetchone.return_value=({'unclassified':[
            {'reference':'TD116518','reason':'Original debit booking not proven'}]},)
        result=r.reconcile(conn)
        self.assertEqual(result['matched_queue_items'],1)
        self.assertEqual(result['conflicts'],[])
        self.assertEqual(next(x for x in result['not_queued'] if x['reference']=='TD116518')['reason'],
                         'Original debit booking not proven')
        self.assertTrue(all(x.args[0].startswith('SELECT') for x in conn.execute.call_args_list))

    def test_suap_start_does_not_reset_existing_cohort_or_completed_icepay(self):
        conn=MagicMock()
        conn.execute.return_value.fetchone.return_value=(p.START_AT,p.START_AT,
            '2026-10-03-allocation-icepay-now-v2',None)
        p.activate_once(conn)
        self.assertTrue(all(x.args[0].startswith('SELECT') for x in conn.execute.call_args_list))
