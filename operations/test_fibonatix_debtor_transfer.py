"""Current user policy supersedes the former Fibonatix transfer authorization."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import bacs_debtor_transfer as m, customer_only_routing as c
from operations import automatic_debtor_routing as a, backfill_debtor_routing as b

ID='00000000-0000-0000-0000-000000000001'
SELECTION={'entry_id':ID,'reference':'TD48874','order_id':137196,'payment_method':'wc_fibonatix'}


class FibonatixRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_retains_fibonatix_without_any_exact_or_audit_calls(self):
        api=AsyncMock();audit=MagicMock()
        result=await c.change_selected(api,SELECTION,{},audit)
        self.assertEqual(result['state'],'retained')
        self.assertEqual(result['destination'],'100100')
        self.assertNotIn('confirmation',result)
        self.assertEqual(api.mock_calls,[]);self.assertEqual(audit.mock_calls,[])

    async def test_new_fibonatix_import_never_resolves_accounts_or_writes(self):
        api=AsyncMock();conn=MagicMock()
        order={'order_id':137196,'order_number':'#48874','payment_method':'wc_fibonatix'}
        with patch.object(c,'route_accounts',AsyncMock()) as accounts,patch.object(c,'change_selected',AsyncMock()) as change:
            self.assertTrue(await a.process_entry(api,conn,ID,'TD48874',order))
        accounts.assert_not_awaited();change.assert_not_awaited()
        self.assertEqual(conn.execute.call_args.args[1][0],'retained')

    async def test_old_waiting_cohort_is_retired_without_reversing_previous_results(self):
        api=AsyncMock();conn=MagicMock()
        with patch.object(c,'route_accounts',AsyncMock()) as accounts,patch.object(c,'change_selected',AsyncMock()) as change:
            self.assertEqual(await b.process_pending(api,conn),'backfill_retained_on_source')
        accounts.assert_not_awaited();change.assert_not_awaited();self.assertEqual(api.mock_calls,[])
        conn.execute.assert_called_once()
        sql,args=conn.execute.call_args.args
        self.assertIn("WHERE state='pending' AND payment_method=%s",sql)
        self.assertEqual(args,('wc_fibonatix',))

    async def test_previous_fibonatix_transfer_plan_is_no_longer_authorized(self):
        p={'version':m.VERSION,'division':m.DIVISION,'source':'100100',
           'destination':'109384','payment_method':'wc_fibonatix',
           'context':{'payment_method':'wc_fibonatix'},'created_at':m.utcnow(),'eligible':[]}
        p['plan_sha256']=m.digest(p);api=AsyncMock()
        with self.assertRaises(m.Stop):await m.apply(api,p,p['plan_sha256'],None,True)
        api.change_customer.assert_not_awaited()

    async def test_active_routes_need_only_icepay_destination(self):
        api=AsyncMock()
        codes=('100100','109419')
        api.rows.return_value=[{'ID':f'00000000-0000-0000-0000-{i:012d}',
            'Code':code,'IsSales':True,'Status':'C'} for i,code in enumerate(codes,1)]
        with patch.object(c,'_accounts',None):
            source=await a.validate_routes(api)
        self.assertEqual(source,'00000000-0000-0000-0000-000000000001')
        self.assertNotIn('109384',str(api.rows.call_args))

    async def test_operator_pause_still_blocks_writes_even_with_enabled_database_switch(self):
        app=MagicMock(DIVISION=m.DIVISION,BASE_URL=m.BASE,COLLECTIVE_DEBTOR_CODE=m.SOURCE)
        conn=MagicMock();conn.execute.return_value.fetchone.return_value=(True,)
        api=a.AutomaticExact(app,conn)
        with patch.object(a,'OPERATOR_PAUSED',True),patch.object(m.Exact,'change_customer',AsyncMock()) as write:
            with self.assertRaises(m.WritePaused):await api.change_customer(ID,ID)
        write.assert_not_awaited()
