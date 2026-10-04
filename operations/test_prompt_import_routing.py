"""Regression coverage for the observed two-hour delay and missed NinjaPay."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

from operations import automatic_debtor_routing as a


class PromptImports(unittest.TestCase):
    def bounds(self, cursor, end):
        return a.scan_params('00000000-0000-0000-0000-000000000001',
            cursor-timedelta(days=1),cursor,end)['$filter']

    def test_live_icepay_import_is_selected_without_two_hour_wait(self):
        # TD49132 Created/Modified 19:45:58 Dutch wall time = 17:45:58Z.
        q=self.bounds(datetime(2026,10,4,17,45,tzinfo=timezone.utc),
                      datetime(2026,10,4,17,46,tzinfo=timezone.utc))
        self.assertIn("Modified ge datetime'2026-10-04T19:43:00'",q)
        self.assertIn("Modified lt datetime'2026-10-04T19:46:00'",q)

    def test_dst_backward_clock_retains_both_folds(self):
        q=self.bounds(datetime(2026,10,25,1,0,tzinfo=timezone.utc),
                      datetime(2026,10,25,1,1,tzinfo=timezone.utc))
        self.assertIn("Modified ge datetime'2026-10-25T01:58:00'",q)
        self.assertIn("Modified lt datetime'2026-10-25T03:01:00'",q)

    def test_winter_offset_and_spring_transition(self):
        q=self.bounds(datetime(2026,12,1,12,tzinfo=timezone.utc),
                      datetime(2026,12,1,12,1,tzinfo=timezone.utc))
        self.assertIn("Modified ge datetime'2026-12-01T12:58:00'",q)
        self.assertIn("Modified lt datetime'2026-12-01T13:01:00'",q)
        q=self.bounds(datetime(2026,3,29,1,tzinfo=timezone.utc),
                      datetime(2026,3,29,1,1,tzinfo=timezone.utc))
        self.assertIn("Modified ge datetime'2026-03-29T01:58:00'",q)
        self.assertIn("Modified lt datetime'2026-03-29T03:01:00'",q)

    def test_clock_migration_catches_backlog_only_once_without_rewinding_cursor(self):
        now=datetime(2026,10,4,18,40,tzinfo=timezone.utc)
        cursor=now-timedelta(minutes=1)
        conn=MagicMock()
        for existing,expected in ((None,(now-timedelta(hours=3),True)),((1,),(cursor,False))):
            conn.execute.return_value.fetchone.return_value=existing
            self.assertEqual(a.scan_cursor(conn,cursor,now),expected)
        self.assertTrue(all(c.args[0].startswith('SELECT') for c in conn.execute.call_args_list))
        conn.execute.return_value.fetchone.return_value=None
        old=now-timedelta(days=1)
        self.assertEqual(a.scan_cursor(conn,old,now),(old,True))

    def test_only_reviewed_skipped_ninjapay_is_requeued(self):
        conn=MagicMock();conn.execute.return_value.rowcount=1
        with patch.object(a.runtime,'event'):
            a.complete_scan_migration(conn)
        query,args=conn.execute.call_args_list[0].args
        self.assertEqual(args,('592a3355-c08e-4dae-9801-f8e62c18f31c',))
        for fragment in ("reference='TD49117'","state='skipped'","work_scope='continuous'",
                         "reason='Payment method outside this work scope'","b.state='uncertain'"):
            self.assertIn(fragment,query)
        self.assertNotIn('jnp_debtor_route_control',query)
        self.assertEqual(len(conn.execute.call_args_list),2)

    def test_poll_interval_and_existing_import_grace(self):
        self.assertEqual(a.sleep_seconds({'state':'watching'}),60)
        self.assertEqual(a.INGEST_GRACE_SECONDS,60)
        conn=MagicMock()
        a.record_state(conn,'entry','pending','Order not yet present in Metorik; no inference',retry_seconds=60)
        self.assertEqual(conn.execute.call_args.args[1][2],60)


if __name__=='__main__':
    unittest.main()
