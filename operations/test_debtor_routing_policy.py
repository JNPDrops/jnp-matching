"""Behavior tests for the fixed route scope, scheduling, and credit identity."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import automatic_debtor_routing as a, debtor_routing_policy as p
from operations import customer_only_routing as c, open_item_cleanup as d
from operations import bacs_debtor_transfer as m, backfill_debtor_routing as b
from operations.test_customer_only_routing import ID, HEADER, OPEN, Audit

ACCOUNTS={code:f'00000000-0000-0000-0000-{i:012d}' for i,code in enumerate(
    ('100100','109372','109377','109419','109421','109422'),10)}
H={**HEADER,'Customer':ACCOUNTS['100100'],'Journal':'70','Currency':'EUR',
   'AmountFC':170,'Description':'Order TD #48874','Modified':'/Date(0)/'}
O={**OPEN,'AccountId':ACCOUNTS['100100'],'JournalCode':'70','CurrencyCode':'EUR'}
ORDER={'order_id':137196,'order_number':'#48874','payment_method':'plisio',
       'currency':'EUR','total':170,'total_refunds':50}
CREDIT_ID='00000000-0000-0000-0000-000000000002'
CREDIT={**H,'EntryID':CREDIT_ID,'YourRef':'TD133225','Type':21,'AmountFC':-50,
        'Description':'Order #48874 / Credit #TD133225'}


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_activation_is_once_and_a_later_pause_is_not_overridden(self):
        for revision,expected in ((None,2),(p.REVISION,0)):
            conn=MagicMock();conn.transaction.return_value=nullcontext()
            conn.execute.return_value.fetchone.return_value=(p.START_AT,p.START_AT,revision,'Paused by authorized operator')
            p.activate_once(conn)
            updates=[x.args[0] for x in conn.execute.call_args_list if x.args[0].startswith('UPDATE')]
            self.assertEqual(len(updates),expected)
            self.assertTrue(all('cursor_at=' not in x and "state='uncertain'" not in x for x in updates))

    def test_start_is_two_am_amsterdam_with_five_second_grace(self):
        from zoneinfo import ZoneInfo
        self.assertEqual(p.START_AT.astimezone(ZoneInfo('Europe/Amsterdam')).isoformat(),'2026-10-04T02:00:05+02:00')
        status={};now=p.START_AT-timedelta(seconds=7)
        self.assertTrue(p.wait_for_start(status,now))
        self.assertEqual(a.sleep_seconds(status,now),7)
        self.assertFalse(p.wait_for_start(status,p.START_AT))
        self.assertEqual(a.sleep_seconds({'state':'processing_queue'},now),15)

    async def test_scheduled_cycle_makes_no_exact_or_metorik_call(self):
        conn=MagicMock();conn.__enter__.return_value=conn
        conn.execute.return_value.fetchone.side_effect=[(True,),(True,p.START_AT,p.START_AT,None)]
        app=MagicMock(DATABASE_URL='configured');app._db_connect.return_value=conn
        with patch.object(a,'initialize'),patch.object(b,'initialize'),patch.object(p,'initialize'),patch.object(p,'activate_once'),patch.object(p,'wait_for_start',return_value=True),patch.object(a,'AutomaticExact') as api,patch.object(a.e,'lookup_orders',AsyncMock()) as lookup:
            await a.cycle(app)
        api.assert_not_called();lookup.assert_not_awaited()

    async def test_all_authorized_routes_only_change_customer_and_accept_partial_balance(self):
        for method,target in p.CLEANUP_ROUTES.items():
            api=AsyncMock();api.limits={'remaining':500}
            api.rows.side_effect=[[H],[{**O,'Amount':0.01}]]
            audit=Audit()
            result=await c.change_selected(api,{'entry_id':ID,'reference':'TD48874',
                'order_id':137196,'payment_method':method,'work_scope':'cleanup'},ACCOUNTS,audit)
            self.assertEqual(result['state'],'applied')
            self.assertEqual(api.rows.await_count,2)
            self.assertEqual(audit.events[0]['payload'],{'Customer':ACCOUNTS[target]})
            api.change_customer.assert_awaited_once_with(ID,ACCOUNTS[target])

    async def test_historical_methods_are_never_applied_to_future_imports(self):
        for method in ('np_payments','suap_wordpresspayplugin'):
            api=AsyncMock()
            with self.assertRaises(m.Stop):
                await c.change_selected(api,{'entry_id':ID,'reference':'TD48874',
                    'order_id':137196,'payment_method':method},ACCOUNTS,Audit())
            self.assertEqual(api.mock_calls,[])

    async def test_write_guard_checks_schedule_policy_pause_and_existing_target(self):
        app=MagicMock(DIVISION=m.DIVISION,BASE_URL=m.BASE,COLLECTIVE_DEBTOR_CODE=m.SOURCE)
        for enabled,revision,waiting,target,allowed in (
            (True,p.REVISION,False,'109421',True),
            (False,p.REVISION,False,'109421',False),
            (True,'superseded',False,'109421',False),
            (True,p.REVISION,True,'109421',False),
            (True,p.REVISION,False,'100100',False)):
            conn=MagicMock();conn.execute.return_value.fetchone.return_value=(enabled,revision)
            api=a.AutomaticExact(app,conn)
            with patch.object(p,'wait_for_start',return_value=waiting),patch.object(c,'route_accounts',AsyncMock(return_value=ACCOUNTS)),patch.object(m.Exact,'change_customer',AsyncMock()) as write:
                if allowed:
                    await api.change_customer(ID,ACCOUNTS[target]);write.assert_awaited_once()
                else:
                    with self.assertRaises(m.Stop):await api.change_customer(ID,ACCOUNTS[target])
                    write.assert_not_awaited()

    async def test_explicit_credit_proof_allows_only_negative_open_remainder(self):
        selection={'entry_id':CREDIT_ID,'reference':'TD133225','order_reference':'TD48874',
                   'order_id':137196,'payment_method':'plisio','work_scope':'cleanup',
                   'entry_type':21,'debit_entry_id':ID}
        for amount,expected in ((-10,'applied'),(0,'skipped'),(10,'skipped')):
            api=AsyncMock();api.limits={'remaining':500}
            api.rows.side_effect=[[CREDIT],[{**O,'YourRef':'TD133225','Amount':amount}]]
            result=await c.change_selected(api,selection,ACCOUNTS,Audit())
            self.assertEqual(result['state'],expected)
            self.assertEqual(api.change_customer.await_count,1 if expected=='applied' else 0)
            self.assertEqual(api.rows.await_count,2)
        for changes in ({'debit_entry_id':None},{'order_reference':'TD133225'},{'work_scope':'continuous'}):
            api=AsyncMock()
            with self.assertRaises(m.Stop):await c.change_selected(api,{**selection,**changes},ACCOUNTS,Audit())
            self.assertEqual(api.mock_calls,[])
        api=AsyncMock();api.limits={'remaining':500}
        api.rows.return_value=[{**CREDIT,'Description':'unproven credit'}]
        result=await c.change_selected(api,selection,ACCOUNTS,Audit())
        self.assertEqual(result['state'],'review');api.change_customer.assert_not_awaited()

    async def test_credit_original_debit_must_be_unique_and_match_webshop(self):
        for headers,order,expected in (([H],ORDER,ID),([H,H],ORDER,None),
            ([{**H,'YourRef':'TD11111'}],ORDER,None),
            ([{**H,'Currency':'USD'}],ORDER,None),
            ([H],{**ORDER,'total_refunds':0},None),
            ([H],{**ORDER,'total':None},None)):
            api=AsyncMock();api.rows.return_value=headers
            self.assertEqual(await d.debit_proof(api,CREDIT,order,'TD48874'),expected)
            self.assertNotIn('Customer',api.rows.await_args.args[1]['$filter'])
            api.change_customer.assert_not_awaited()

    async def test_cleanup_uses_current_open_population_once_without_age_limit(self):
        conn=MagicMock();conn.transaction.return_value=nullcontext()
        saved=None
        def sql(query,args=None):
            nonlocal saved
            result=MagicMock()
            if query.startswith('SELECT open_item_cleanup'):result.fetchone.return_value=(saved,)
            elif query.startswith('UPDATE jnp_debtor_route_control SET open_item_cleanup'):saved=json.loads(args[0])
            elif query.startswith('SELECT EXISTS'):result.fetchone.return_value=(False,)
            elif 'GROUP BY state' in query:result.fetchall.return_value=[]
            return result
        conn.execute.side_effect=sql
        api=AsyncMock();api.rows.side_effect=[[O],[H]]
        with patch.object(d.e,'lookup_orders',AsyncMock(return_value={'orders':{'#48874':ORDER}})):
            await d.discover_batch(api,conn,ACCOUNTS['100100'])
            await d.discover_batch(api,conn,ACCOUNTS['100100'])
        self.assertEqual(api.rows.await_count,2)
        self.assertNotIn('Date',api.rows.await_args_list[0].args[1]['$filter'])
        self.assertTrue(saved['done']);self.assertEqual(saved['cursor'],1)
        inserts=[x for x in conn.execute.call_args_list if x.args[0].startswith('INSERT INTO jnp_debtor_route_queue')]
        self.assertEqual(len(inserts),1)
        self.assertEqual(json.loads(inserts[0].args[1][3]),{k:ORDER[k] for k in ('order_id','order_number','payment_method')})
        self.assertIn("state<>'uncertain'",inserts[0].args[0])
        api.change_customer.assert_not_awaited()

    def test_paid_ambiguous_and_nonwebshop_items_do_not_enter_cleanup(self):
        items=[O,{**O,'YourRef':'TD10000','Amount':0},{**O,'YourRef':'manual'},
               {**O,'YourRef':'TD20000'},{**O,'YourRef':'TD20000'},
               {**O,'YourRef':'TD30000','Amount':-50}]
        result=d.candidates(items,ACCOUNTS['100100'])
        self.assertEqual({r['reference'] for r in result},{'TD48874','TD30000'})
        self.assertEqual(d.order_reference(CREDIT),'TD48874')
        self.assertIsNone(d.order_reference({**CREDIT,'Description':'Order #48874 / Credit #TD99999'}))
