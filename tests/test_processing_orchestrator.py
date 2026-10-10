import unittest
from operations.processing_orchestrator import STAGES, choose_stage


class ProcessingOrderTests(unittest.TestCase):
    def test_first_source_is_fibonatix(self):
        self.assertEqual(choose_stage({}), ('fibonatix_source','fibonatix',[]))

    def test_running_job_prevents_second_writer(self):
        self.assertIsNone(choose_stage({'fibonatix_source':'queued'}))

    def test_failed_source_blocks_its_import(self):
        self.assertEqual(choose_stage({'fibonatix_source':'blocked'}),
                         ('fibonatix_import','fibonatix',['fibonatix_source']))

    def test_independent_psp_is_not_skipped(self):
        self.assertEqual(choose_stage({'fibonatix_source':'blocked','fibonatix_import':'blocked'}),
                         ('icepay_source','icepay',[]))

    def test_automatically_requires_routing_and_all_maintenance(self):
        states={name:'verified' for name,_ in STAGES[:8]}
        for missing in ('fibonatix_import','routing','woo_rules','tax','maintenance'):
            with self.subTest(missing=missing):
                self.assertEqual(choose_stage({**states,missing:'blocked'}),
                                 ('fibonatix_automatically','fibonatix',[missing]))

    def test_report_never_passes_pending_matching(self):
        states={name:'verified' for name,_ in STAGES[:9]}
        states['icepay_automatically']='queued'
        self.assertIsNone(choose_stage(states))

    def test_incomplete_report_only_after_all_prior_stages_terminal(self):
        states={name:'verified' for name,_ in STAGES[:-1]}
        states['fibonatix_automatically']='blocked'
        self.assertEqual(choose_stage(states),('report','reports',[]))

    def test_verified_stages_never_repeated(self):
        self.assertIsNone(choose_stage({name:'verified' for name,_ in STAGES}))
