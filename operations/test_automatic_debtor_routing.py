import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from operations import automatic_debtor_routing as a, bacs_debtor_transfer as m, metorik_bacs_evidence as e
from operations.test_plisio_debtor_transfer import fixture
from operations import customer_only_routing as c


class ScopeTests(unittest.TestCase):
    def test_incremental_scope_requires_source_and_new_creation_with_overlap(self):
        start=datetime(2026,10,3,tzinfo=timezone.utc)
        params=a.scan_params('00000000-0000-0000-0000-000000000001',start,start+timedelta(minutes=20),start+timedelta(minutes=25))
        self.assertIn("Created ge datetime'2026-10-03T00:00:00'",params['$filter'])
        self.assertIn("Modified ge datetime'2026-10-03T00:18:00'",params['$filter'])
        self.assertIn("Modified lt datetime'2026-10-03T00:25:00'",params['$filter'])
        with self.assertRaises(m.Stop): a.scan_params('bad',start,start,start)

    def test_old_entry_rejected_before_queue_or_cursor_change(self):
        conn=MagicMock()
        conn.transaction.return_value=nullcontext()
        row={'EntryID':'00000000-0000-0000-0000-000000000001','Customer':'source','Created':'/Date(0)/'}
        with self.assertRaises(m.Stop):a.enqueue(conn,[row],'source',datetime.now(timezone.utc),datetime.now(timezone.utc))
        conn.execute.assert_not_called()

    def test_durable_intent_precedes_put_and_complete_marks_verified(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        audit=a.Audit(conn,'00000000-0000-0000-0000-000000000001')
        m.append_audit(audit,{'event':'write_intent','before':{'Customer':'source'}})
        self.assertTrue(audit.write_started)
        self.assertIn("state='uncertain'",conn.execute.call_args.args[0])
        m.append_audit(audit,{'event':'complete','moved':[]})
        self.assertIn("state='verified'",conn.execute.call_args.args[0])

    def test_failed_intent_persistence_does_not_report_success(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext();conn.execute.side_effect=RuntimeError('database down')
        audit=a.Audit(conn,'00000000-0000-0000-0000-000000000001')
        with self.assertRaises(RuntimeError):audit.persist_event({'event':'write_intent'})
        self.assertFalse(audit.write_started)

    def test_global_audit_keeps_totals_and_hash_without_repeating_all_rows(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        audit=a.Audit(conn,'00000000-0000-0000-0000-000000000001')
        rows=[{'AccountCode':' 100100','CurrencyCode':'EUR','Amount':25}]
        audit.persist_event({'event':'balances_before','rows':rows})
        import json
        body=json.loads(conn.execute.call_args.args[1][3])
        self.assertEqual(body['totals'],{'100100/EUR':'25'})
        self.assertEqual(body['population_sha256'],m.digest(rows))
        self.assertNotIn('rows',body)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_methods_never_build_a_write_plan(self):
        with patch.object(e,'evidence_plan',AsyncMock()) as build:
            await a.process_entry(AsyncMock(),Mock(),'entry','TD12345',{'payment_method':'card'})
            build.assert_not_awaited()

    async def test_existing_order_evidence_is_reused_without_full_plan_or_balance_reads(self):
        conn=Mock()
        with patch.object(e,'evidence_plan',AsyncMock()) as planner,patch.object(m,'apply',AsyncMock()) as old_apply,patch.object(c,'route_accounts',AsyncMock(return_value={})),patch.object(c,'change_selected',AsyncMock(return_value={'state':'skipped','reason':'No remaining open item'})) as change:
            await a.process_entry(AsyncMock(),conn,'entry','TD12345',{'payment_method':'plisio','order_id':7,'order_number':'#12345'})
            planner.assert_not_awaited();old_apply.assert_not_awaited();change.assert_awaited_once()
            self.assertEqual(change.await_args.args[1],{'entry_id':'entry','reference':'TD12345','order_id':7,'payment_method':'plisio'})
            self.assertEqual(conn.execute.call_args.args[1],('skipped','No remaining open item','entry'))

    async def test_any_failure_after_intent_pauses_automatic_routing(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        async def failed(api,selection,accounts,audit):
            audit.persist_event({'event':'write_intent'})
            raise m.Stop('ambiguous response')
        with patch.object(c,'route_accounts',AsyncMock(return_value={})),patch.object(c,'change_selected',side_effect=failed):
            with self.assertRaises(m.Stop):
                await a.process_entry(AsyncMock(),conn,'00000000-0000-0000-0000-000000000001','TD12345',{'payment_method':'plisio','order_id':7,'order_number':'#12345'})
        self.assertTrue(any('enabled=FALSE' in c.args[0] for c in conn.execute.call_args_list))

    async def test_restart_with_uncertain_entry_never_queries_exact(self):
        conn=MagicMock();conn.__enter__.return_value=conn
        conn.execute.return_value.fetchone.side_effect=[(True,),(True,datetime.now(timezone.utc),datetime.now(timezone.utc)),(True,)]
        app=Mock(DATABASE_URL='configured');app._db_connect.return_value=conn
        with patch.object(a,'initialize'),patch.object(a.Path,'open',MagicMock()),patch.object(a.fcntl,'flock'),patch.object(a.m,'Exact') as exact:
            await a.cycle(app)
            exact.assert_not_called()
        self.assertTrue(any('enabled=FALSE' in c.args[0] for c in conn.execute.call_args_list))

    async def test_missing_orders_are_allowed_in_discovery_not_in_write_proof(self):
        with patch.object(e,'lookup_orders',AsyncMock(return_value={'store':{},'orders':{}})):
            with self.assertRaises(m.Stop):
                await e.read_orders([{'entry_id':'00000000-0000-0000-0000-000000000001','reference':'TD12345','order_id':7}])


if __name__=='__main__':unittest.main()

class PauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_after_preflight_blocks_customer_put(self):
        app=Mock(DIVISION=m.DIVISION,BASE_URL=m.BASE,COLLECTIVE_DEBTOR_CODE=m.SOURCE)
        conn=MagicMock();conn.execute.return_value.fetchone.return_value=(False,)
        api=a.AutomaticExact(app,conn)
        with patch.object(m.Exact,'change_customer',AsyncMock()) as write:
            with self.assertRaises(m.Stop):await api.change_customer('entry','target')
            write.assert_not_awaited()
