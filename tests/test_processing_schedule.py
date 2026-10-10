import unittest
from datetime import date,datetime,timedelta,timezone
from operations.processing_schedule import ZONE,batch_at,due_batches,register_due
from operations.routing_completion import RoutingNotReady,validate_proof
from operations.processing_alerts import config,message


class CalendarTests(unittest.TestCase):
    def test_intraday_uses_previous_hour_same_day(self):
        for hour in (9,13,17,21):
            b=batch_at(datetime(2026,10,10,hour,tzinfo=ZONE))
            self.assertEqual(b.processing_date,date(2026,10,10))
            self.assertEqual(b.cutoff.astimezone(ZONE).hour,hour-1)
            self.assertFalse(b.final)

    def test_night_closes_previous_calendar_day(self):
        b=batch_at(datetime(2026,10,11,1,tzinfo=ZONE))
        self.assertEqual(b.processing_date,date(2026,10,10))
        self.assertEqual(b.cutoff.astimezone(ZONE),datetime(2026,10,11,tzinfo=ZONE))
        self.assertTrue(b.final)

    def test_dst_day_has_23_or_25_hours(self):
        for month,day,hours in ((3,30,23),(10,26,25)):
            b=batch_at(datetime(2026,month,day,1,tzinfo=ZONE))
            self.assertEqual((b.cutoff-b.start).total_seconds(),hours*3600)

    def test_outage_preserves_all_prior_slots(self):
        prior=list(due_batches(date(2026,10,8),datetime(2026,10,10,14,tzinfo=ZONE)))
        self.assertEqual(len(prior),13)
        self.assertEqual(prior[0].processing_date,date(2026,10,7))
        self.assertEqual(prior[-1].planned_at.hour,13)
        self.assertEqual(len({b.key for b in prior}),13)

    def test_not_due_early(self):
        self.assertEqual(list(due_batches(date(2026,10,10),datetime(2026,10,10,0,59,tzinfo=ZONE))),[])

    def test_rejects_non_slot_and_naive_times(self):
        for dt in (datetime(2026,10,10,8,tzinfo=ZONE),datetime(2026,10,10,9),datetime(2026,10,10,9,1,tzinfo=ZONE)):
            with self.assertRaises(ValueError):batch_at(dt)

    def test_timezone_representation_does_not_change_key(self):
        dt=datetime(2026,10,10,9,tzinfo=ZONE)
        self.assertEqual(batch_at(dt),batch_at(dt.astimezone(timezone.utc)))


class RoutingTests(unittest.TestCase):
    now=datetime(2026,10,10,10,tzinfo=timezone.utc)
    def test_fifteen_minutes_inclusive(self):
        p={'completed_at':self.now-timedelta(minutes=15),'scanned_through':self.now-timedelta(hours=1)}
        self.assertIs(validate_proof(p,self.now,p['scanned_through']),p)

    def test_stale_future_and_missing_rejected(self):
        for p in (None,{'completed_at':self.now-timedelta(minutes=15,seconds=1)}, {'completed_at':self.now+timedelta(seconds=1)}):
            with self.assertRaises(RoutingNotReady):validate_proof(p,self.now)

    def test_recent_finish_does_not_prove_cutoff_coverage(self):
        p={'completed_at':self.now,'scanned_through':self.now-timedelta(hours=2)}
        with self.assertRaisesRegex(RoutingNotReady,'batch_cutoff'):
            validate_proof(p,self.now,self.now-timedelta(hours=1))


class NotificationTests(unittest.TestCase):
    def test_missing_delivery_config_explicit(self):
        with self.assertRaisesRegex(ValueError,'not_configured'):config({})

    def test_header_injection_rejected(self):
        c=dict(JNP_SMTP_HOST='example.test',JNP_SMTP_USER='test',JNP_SMTP_PASSWORD='synthetic',JNP_ALERT_FROM='sender@example.test',JNP_ALERT_TO='user@example.test\nBcc:other@example.test')
        with self.assertRaises(ValueError):config(c)

    def test_mail_excludes_credentials(self):
        c=dict(JNP_SMTP_HOST='example.test',JNP_SMTP_USER='test',JNP_SMTP_PASSWORD='synthetic-secret',JNP_ALERT_FROM='sender@example.test',JNP_ALERT_TO='user@example.test')
        m=message(config(c),'abc','jnp:3977752:batch:2026-10-10T0900','routing','routing_stale')
        self.assertNotIn('synthetic-secret',m.as_string())
        self.assertIn('routing_stale',m.get_content())


if __name__=='__main__':unittest.main()
