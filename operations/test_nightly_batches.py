from datetime import datetime, date, timedelta
import unittest
from operations.nightly_batches import window, identity, report_gate, missing_receipts, STAGES


class NightlyTests(unittest.TestCase):
    def test_first_day(self):
        day, lo, hi = window(datetime.fromisoformat('2026-10-07T01:00:00+02:00'))
        self.assertEqual(day,date(2026,10,6))
        self.assertEqual(lo.isoformat(),'2026-10-05T22:00:00+00:00')
        self.assertEqual(hi.isoformat(),'2026-10-06T22:00:00+00:00')

    def test_dst_days_are_not_fixed_24_hours(self):
        for start,hours in [('2026-03-30T01:00:00+02:00',23),('2026-10-26T01:00:00+01:00',25)]:
            _,lo,hi = window(datetime.fromisoformat(start))
            self.assertEqual(hi-lo,timedelta(hours=hours))

    def test_naive_start_rejected(self):
        with self.assertRaises(ValueError):window(datetime(2026,10,7,1))

    def test_psp_identity_is_stable_and_distinct(self):
        self.assertEqual(identity(date(2026,10,6),'icepay'),'jnp:3977752:icepay:2026-10-06')
        self.assertNotEqual(identity(date(2026,10,6),'icepay'),identity(date(2026,10,6),'fibonatix'))

    def test_report_cannot_pass_running_or_unknown_stage(self):
        for state in ('pending','running','uncertain'):
            stages = {s:'completed' for s in STAGES[:-1]}
            stages['tax']=state
            with self.assertRaises(ValueError):report_gate(stages)
        with self.assertRaises(ValueError):report_gate({})

    def test_report_distinguishes_incomplete(self):
        stages = {s:'completed' for s in STAGES[:-1]}
        self.assertEqual(report_gate(stages),'completed')
        stages['fibonatix']='blocked'
        self.assertEqual(report_gate(stages),'incomplete')

    def test_identical_import_dedup_and_conflict(self):
        a={'payment_id':'a','amount':'10.00'}
        b={'payment_id':'b','amount':'20.00'}
        self.assertEqual(missing_receipts([a,b],{'a':a}),[b])
        with self.assertRaises(ValueError):missing_receipts([a,a],{})
        with self.assertRaises(ValueError):missing_receipts([a],{'a':dict(a,amount='11.00')})


if __name__ == '__main__':unittest.main()
