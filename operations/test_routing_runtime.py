from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

from operations import routing_runtime as r, customer_only_routing as c, bacs_debtor_transfer as m


class RecoveryTests(unittest.TestCase):
    def connection(self, recovered=None, initialized=True):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        now=datetime.now(timezone.utc) if initialized else None
        conn.execute.return_value.fetchone.return_value=(False,now,now,
            'Backfill write attempted without a complete verified audit; inspect durable archive',recovered)
        return conn

    def test_resume_once_never_rewinds_cursor_or_requeues_uncertain_entries(self):
        conn=self.connection()
        with patch.object(r,'queue_counts',return_value={'backfill':{'uncertain':1}}),patch.object(r,'event') as event:
            r.recover_once(conn)
        writes=[call.args[0] for call in conn.execute.call_args_list if call.args[0].startswith('UPDATE')]
        self.assertEqual(len(writes),1)
        self.assertIn('enabled=TRUE',writes[0]);self.assertNotIn('cursor_at=',writes[0])
        self.assertNotIn('route_backfill',writes[0]);self.assertNotIn('route_queue',writes[0])
        self.assertEqual(event.call_args.kwargs['previous_pause'],'backfill_entry_unconfirmed')

    def test_later_manual_pause_and_uninitialized_service_are_never_overridden(self):
        for conn in (self.connection(recovered=datetime.now(timezone.utc)),self.connection(initialized=False)):
            r.recover_once(conn)
            self.assertFalse(any(call.args[0].startswith('UPDATE') for call in conn.execute.call_args_list))

    def test_daily_limit_waits_until_reset_then_resumes(self):
        status={};reset=datetime.now(timezone.utc)+timedelta(hours=2)
        r.defer_until_reset(status,{'remaining':0,'reset_ms':int(reset.timestamp()*1000)})
        self.assertTrue(r.deferred(status))
        self.assertGreater(datetime.fromisoformat(status['next_attempt_at']),reset)
        status['next_attempt_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.assertFalse(r.deferred(status));self.assertIsNone(status['next_attempt_at'])

    def test_backfill_reserve_does_not_block_new_imports_until_tomorrow(self):
        status={};now=datetime.now(timezone.utc)
        r.defer_until_reset(status,{'remaining':120,'reset_ms':int((now+timedelta(hours=12)).timestamp()*1000)})
        self.assertLess(datetime.fromisoformat(status['next_attempt_at']),now+timedelta(minutes=6))

    def test_explicit_rate_rejection_can_retry_but_ambiguous_writes_cannot(self):
        self.assertEqual(c.failure_result(m.ExactRequestError('PUT',429),True)['state'],'pending')
        self.assertTrue(c.failure_result(m.ExactRequestError('PUT',429),True)['stop_cycle'])
        for exc in (m.ExactRequestError('PUT',500),m.Stop('Exact PUT transport/auth failure; inspect audit before retrying')):
            self.assertEqual(c.failure_result(exc,True)['state'],'uncertain')
        self.assertEqual(c.failure_result(m.ExactRequestError('PUT',400),True)['state'],'review')
        self.assertEqual(c.failure_result(m.WritePaused('stopped'),True)['state'],'pending')

