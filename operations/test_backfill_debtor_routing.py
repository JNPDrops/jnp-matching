from contextlib import nullcontext
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import backfill_debtor_routing as b, automatic_debtor_routing as a
from operations import bacs_debtor_transfer as m, metorik_bacs_evidence as e

ID='00000000-0000-0000-0000-000000000001'
OTHER='00000000-0000-0000-0000-000000000002'


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        conn.execute.return_value.fetchall.return_value=[(ID,'TD12345',7,b.METHOD)]
        return conn

    async def test_low_or_unknown_budget_does_not_even_build_a_plan(self):
        for limits in ({},{'remaining':1599}):
            api=MagicMock(limits=limits)
            with patch.object(e,'evidence_plan',AsyncMock()) as planner:
                self.assertEqual(await b.process_pending(api,self.connection()),'backfill_waiting_for_api_budget')
                planner.assert_not_awaited()

    async def test_budget_is_checked_again_after_read_only_plan(self):
        api=MagicMock(limits={'remaining':2000})
        async def plan(*args):
            api.limits={'remaining':1500}
            return {'review':[],'paid_skipped':[],'eligible':[{'entry_id':ID}]}
        with patch.object(e,'evidence_plan',side_effect=plan),patch.object(m,'apply',AsyncMock()) as apply:
            self.assertEqual(await b.process_pending(api,self.connection()),'backfill_waiting_for_api_budget')
            apply.assert_not_awaited()

    async def test_paid_reviewed_and_wrong_method_never_write(self):
        conn=self.connection();api=MagicMock(limits={'remaining':3000})
        plan={'review':[{'entry_id':ID,'reason':'partial'}],'paid_skipped':[OTHER],'eligible':[]}
        with patch.object(e,'evidence_plan',AsyncMock(return_value=plan)),patch.object(m,'apply',AsyncMock()) as apply:
            await b.process_pending(api,conn)
            apply.assert_not_awaited()
        conn.execute.return_value.fetchall.return_value=[(ID,'TD12345',7,'plisio')]
        with self.assertRaises(m.Stop): await b.process_pending(api,conn)

    async def test_failed_write_pauses_instead_of_replaying(self):
        conn=self.connection();api=MagicMock(limits={'remaining':3000})
        plan={'plan_sha256':'sha','review':[],'paid_skipped':[],'eligible':[{'entry_id':ID}]}
        async def failure(api,p,sha,audit,**kw):
            audit.persist_event({'event':'write_intent','entry_id':ID})
            raise m.Stop('ambiguous outcome')
        with patch.object(e,'evidence_plan',AsyncMock(return_value=plan)),patch.object(m,'apply',side_effect=failure),patch.object(a,'pause') as pause:
            with self.assertRaises(m.Stop): await b.process_pending(api,conn)
            pause.assert_called_once()

    async def test_import_checksum_rejects_unapproved_cohort(self):
        app=MagicMock();conn=self.connection()
        app._db_connect.return_value.__enter__.return_value=conn
        conn.execute.return_value.fetchone.side_effect=[(True,),(b.METHOD,{}, {})]
        with patch.object(m,'context',AsyncMock()),patch.object(m,'Exact'),patch.object(m,'persistent_runner_lock',return_value=nullcontext()):
            with self.assertRaises(m.Stop): await b.seed(app,'a'*64)
        self.assertFalse(any('INSERT INTO jnp_debtor_route_backfill' in call.args[0] for call in conn.execute.call_args_list))

    async def test_all_route_contexts_preserve_fixed_destinations_in_two_reads(self):
        api=AsyncMock()
        codes=('100100','109372','109377','109384')
        accounts=[{'Code':code.rjust(18),'Name':code,'ID':f'00000000-0000-0000-0000-{i:012d}','IsSales':True,'Status':'C'} for i,code in enumerate(codes,1)]
        conditions=[{'Code':route[1],'Description':method,'PaymentMethod':'B'} for method,route in m.ROUTES.items()]
        api.rows.side_effect=[accounts,conditions]
        ctx=await m.route_contexts(api)
        self.assertEqual({method:m.destination(c) for method,c in ctx.items()}, {'bacs':'109372','plisio':'109377','wc_fibonatix':'109384'})
        self.assertEqual(api.rows.await_count,2)


class AuditTests(unittest.TestCase):
    def test_durable_multi_entry_intent_and_completion(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        audit=b.Audit(conn,{'plan_sha256':'sha','eligible':[{'entry_id':ID},{'entry_id':OTHER}]})
        audit.persist_event({'event':'write_intent','entry_id':OTHER})
        self.assertTrue(audit.write_started)
        self.assertIn("state='uncertain'",conn.execute.call_args.args[0])
        self.assertEqual(conn.execute.call_args.args[1],(OTHER,))
        with self.assertRaises(m.Stop): audit.persist_event({'event':'complete','moved':[{'entry_id':ID}]})
        audit.persist_event({'event':'complete','moved':[{'entry_id':ID},{'entry_id':OTHER}]})
        verified=[c.args[1][0] for c in conn.execute.call_args_list if "state='verified'" in c.args[0]]
        self.assertEqual(set(verified),{ID,OTHER})
        self.assertEqual(json.loads(conn.execute.call_args.args[1][0])['kind'],'automatic_backfill')

    def test_failed_persistence_never_claims_intent_written(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        audit=b.Audit(conn,{'plan_sha256':'sha','eligible':[{'entry_id':ID}]})
        conn.execute.side_effect=RuntimeError('storage failure')
        with self.assertRaises(RuntimeError): audit.persist_event({'event':'write_intent','entry_id':ID})
        self.assertFalse(audit.write_started)
