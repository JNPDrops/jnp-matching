import unittest
from unittest.mock import MagicMock

from operations import reviewed_routing_updates as r, bacs_debtor_transfer as m


class RevisedRoutingTests(unittest.TestCase):
    def test_reclassified_ninja_bacs_and_plisio_are_bound_to_the_reviewed_order(self):
        for method in ('np_payments','bacs','plisio'):
            row=next(x for x in r.ENTRIES.values() if x['payment_method']==method)
            r.validate_selection(row)
            for change in ({'payment_method':'suap_wordpresspayplugin'},
                           {'order_reference':'TD99999'},{'order_id':1}):
                with self.assertRaises(m.Stop):r.validate_selection({**row,**change})

    def test_unknowns_are_never_inferred_and_new_orders_keep_live_proof(self):
        row=next(x for x in r.ENTRIES.values() if x['reference']=='TD40913')
        with self.assertRaises(m.Stop):r.validate_selection({**row,'payment_method':'bacs'})
        r.validate_selection({'entry_id':'00000000-0000-0000-0000-000000000001'})

    def test_reconciliation_reports_unproven_credit_without_requeueing_it(self):
        conn=MagicMock();conn.execute.return_value.fetchall.return_value=[]
        conn.execute.return_value.fetchone.return_value=({'unclassified':[
            {'reference':'TD67066','reason':'Original debit booking not proven'}]},)
        result=r.reconcile(conn)
        credit=next(x for x in result['not_queued_authorized_methods'] if x['reference']=='TD67066')
        self.assertEqual(credit['payment_method'],'np_payments')
        self.assertEqual(credit['reason'],'Original debit booking not proven')
        self.assertTrue(all(x.args[0].startswith('SELECT') for x in conn.execute.call_args_list))
