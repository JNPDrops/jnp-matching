import unittest
from datetime import date,datetime,timezone
from operations.processing_orchestrator import STAGES
from operations.processing_reports import report_data


class ReportGateTests(unittest.TestCase):
    def setUp(self):
        self.args=(date(2026,10,9),datetime(2026,10,8,22,tzinfo=timezone.utc),datetime(2026,10,9,22,tzinfo=timezone.utc))
        self.stages=[(s,'verified',{'proof':True}) for s,_ in STAGES if s!='report']

    def test_running_pending_or_missing_stage_prevents_report(self):
        for state in ('pending','queued'):
            with self.subTest(state=state),self.assertRaisesRegex(ValueError,'not_terminal'):
                report_data(*self.args,self.stages[:-1]+[(self.stages[-1][0],state,{})])
        with self.assertRaisesRegex(ValueError,'not_terminal'):
            report_data(*self.args,self.stages[:-1])

    def test_blocked_or_uncertain_day_is_explicitly_incomplete(self):
        for state in ('blocked','uncertain'):
            data=report_data(*self.args,[(self.stages[0][0],state,{'reason':'synthetic_block'})]+self.stages[1:])
            self.assertEqual(data['state'],'incomplete')
            self.assertFalse(data['financial_writes'])

    def test_only_all_verified_evidence_is_complete(self):
        self.assertEqual(report_data(*self.args,self.stages)['state'],'complete')

    def test_acquired_but_unprocessed_source_exception_keeps_report_incomplete(self):
        for evidence in ({'unprocessed_source_items':1},{'unprocessed_refunds':1}):
            stages=[(stage,state,evidence if stage=='icepay_source' else old) for stage,state,old in self.stages]
            self.assertEqual(report_data(*self.args,stages)['state'],'incomplete')
