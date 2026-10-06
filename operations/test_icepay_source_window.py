import unittest
from datetime import date
from unittest.mock import Mock, patch
from operations import icepay_source_window as w
from operations import icepay_transactions as t
from operations.test_icepay_transactions import fixture, ROW

class WindowTests(unittest.TestCase):
    def test_fresh_identity_excludes_old_and_future_periods(self):
        self.assertEqual(w.identity(date(2026,10,4),date(2026,10,5)), 'icepay-source-20261004-20261005-v1')
        for start,end in [(3,5),(4,6),(5,4)]:
            with self.assertRaises(ValueError): w.identity(date(2026,10,start),date(2026,10,end))

    def test_new_window_preserves_legacy_constants_and_default_validation(self):
        row=ROW.copy(); row[2]='10/04/2026 1:00:00 PM'
        with self.assertRaises(t.AcquisitionStopped): t.parse_payments(fixture([row]),1)
        selected,summary=t.parse_payments(fixture([row]),1,['123'],utc_to_amsterdam=True,start=date(2026,10,4),end=date(2026,10,5))
        self.assertEqual(selected[0]['date'],'2026-10-04')
        self.assertEqual(summary['per_day'],{'2026-10-04':{'count':1,'total':'24.95'}})
        self.assertEqual((t.START,t.END),(date(2026,10,1),date(2026,10,3)))

    def test_exclusive_amsterdam_cutoff(self):
        row=ROW.copy(); row[2]='10/05/2026 10:00:00 PM'
        with self.assertRaises(t.AcquisitionStopped):
            t.parse_payments(fixture([row]),1,['123'],utc_to_amsterdam=True,start=date(2026,10,4),end=date(2026,10,5))
        row[2]='10/05/2026 9:59:59 PM'
        self.assertEqual(t.parse_payments(fixture([row]),1,['123'],utc_to_amsterdam=True,start=date(2026,10,4),end=date(2026,10,5))[0][0]['date'],'2026-10-05')

    def test_timezone_must_be_proved_again_for_new_export(self):
        with patch.object(t,'verify_csv_timezone',return_value={'verified':False}), patch.object(t,'parse_payments') as parse:
            with self.assertRaises(t.AcquisitionStopped):
                w.validate_source(b'csv',['123'],{},date(2026,10,4),date(2026,10,5))
            parse.assert_not_called()

    def test_duplicate_claim_never_overwrites_existing_task(self):
        conn=Mock(); conn.execute.return_value.fetchone.return_value=None
        self.assertFalse(w.claim(conn,'icepay-source-20261004-20261005-v1'))
        sql,args=conn.execute.call_args.args
        self.assertIn('ON CONFLICT DO NOTHING',sql)
        self.assertNotIn('UPDATE',sql)

    def test_new_window_still_rejects_wrong_source_ids(self):
        row=ROW.copy(); row[2]='10/04/2026 1:00:00 PM'
        with self.assertRaises(t.AcquisitionStopped):
            t.parse_payments(fixture([row]),1,['456'],start=date(2026,10,4),end=date(2026,10,5))

if __name__=='__main__': unittest.main()
