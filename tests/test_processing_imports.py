import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from operations.processing_imports import bounded_rows,digest


class CutoffTests(unittest.TestCase):
    def row(self,identifier,stamp,**other):
        return {'payment_id':identifier,'source_time_utc':stamp,**other}

    def test_exact_cutoff_is_exclusive_and_start_inclusive(self):
        start=datetime(2026,10,10,0,tzinfo=timezone.utc)
        end=start+timedelta(hours=6)
        rows=[self.row('before',(start-timedelta(microseconds=1)).isoformat()),
              self.row('start',start.isoformat()),self.row('end',end.isoformat())]
        self.assertEqual([x['payment_id'] for x in bounded_rows(rows,start,end)],['start'])

    def test_winter_time_day_includes_both_repeated_hours(self):
        zone=ZoneInfo('Europe/Amsterdam')
        start=datetime(2026,10,25,tzinfo=zone).astimezone(timezone.utc)
        end=datetime(2026,10,26,tzinfo=zone).astimezone(timezone.utc)
        rows=[self.row('first','2026-10-25T02:30:00+02:00'),
              self.row('second','2026-10-25T02:30:00+01:00')]
        self.assertEqual(len(bounded_rows(rows,start,end)),2)
        self.assertEqual(end-start,timedelta(hours=25))

    def test_timezone_is_required(self):
        with self.assertRaisesRegex(ValueError,'must_be_aware'):
            bounded_rows([self.row('one','2026-10-10T05:00:00')],
                         datetime(2026,10,10,tzinfo=timezone.utc),datetime(2026,10,11,tzinfo=timezone.utc))

    def test_duplicate_psp_identity_is_rejected_even_outside_window(self):
        rows=[self.row('one','2026-10-09T00:00:00Z'),self.row('one','2026-10-10T05:00:00Z')]
        with self.assertRaisesRegex(ValueError,'duplicate_source_payment_id'):
            bounded_rows(rows,datetime(2026,10,10,tzinfo=timezone.utc),datetime(2026,10,11,tzinfo=timezone.utc))

    def test_order_status_does_not_remove_a_confirmed_source_receipt(self):
        rows=[self.row(str(i),'2026-10-10T05:00:00Z',order_status=status)
              for i,status in enumerate(('processing','pending','cancelled','refunded','failed'))]
        self.assertEqual(bounded_rows(rows,datetime(2026,10,10,tzinfo=timezone.utc),datetime(2026,10,11,tzinfo=timezone.utc)),rows)

    def test_evidence_digest_is_stable_but_detects_amount_change(self):
        self.assertEqual(digest({'id':1,'amount':'2.00'}),digest({'amount':'2.00','id':1}))
        self.assertNotEqual(digest({'id':1,'amount':'2.00'}),digest({'id':1,'amount':'2.01'}))
