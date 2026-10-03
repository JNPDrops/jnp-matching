from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from operations import customer_only_routing as c, bacs_debtor_transfer as m

ID='00000000-0000-0000-0000-000000000001'
ACCOUNTS={code:f'00000000-0000-0000-0000-{i:012d}' for i,code in enumerate(('100100','109372','109377'),10)}
SELECTION={'entry_id':ID,'reference':'TD48874','order_id':137196,'payment_method':'plisio'}
HEADER={'EntryID':ID,'Customer':ACCOUNTS['100100'],'YourRef':'TD48874','EntryNumber':26722913,'Status':20,'Type':20,'Reversal':False}
OPEN={'AccountId':ACCOUNTS['100100'],'EntryNumber':26722913,'YourRef':'TD48874','Amount':170}

class Audit:
    def __init__(self): self.events=[]
    def persist_event(self,event): self.events.append(event)

class CustomerOnlyTests(unittest.IsolatedAsyncioTestCase):
    def api(self,header=None,items=None):
        api=AsyncMock();api.limits={'remaining':500}
        api.rows.side_effect=[[deepcopy(HEADER if header is None else header)],deepcopy([OPEN] if items is None else items)]
        return api

    async def test_all_routes_use_two_targeted_reads_and_customer_only_write(self):
        for method,(code,_) in m.ROUTES.items():
            api=self.api();audit=Audit()
            async def write(entry,target):
                self.assertEqual(audit.events[-1]['event'],'write_intent')
            api.change_customer.side_effect=write
            result=await c.change_selected(api,{**SELECTION,'payment_method':method},ACCOUNTS,audit)
            self.assertEqual(result['state'],'applied')
            self.assertEqual(api.rows.await_count,2)
            self.assertEqual([x.args[0] for x in api.rows.await_args_list],['salesentry/SalesEntries','read/financial/ReceivablesList'])
            api.change_customer.assert_awaited_once_with(ID,ACCOUNTS[code])
            self.assertEqual(audit.events[0]['payload'],{'Customer':ACCOUNTS[code]})
            self.assertEqual(audit.events[-1]['event'],'customer_applied')

    async def test_already_moved_is_not_written_again(self):
        api=self.api({**HEADER,'Customer':ACCOUNTS['109377']})
        result=await c.change_selected(api,SELECTION,ACCOUNTS,Audit())
        self.assertEqual(result['state'],'applied');self.assertEqual(api.rows.await_count,1)
        api.change_customer.assert_not_awaited()

    async def test_paid_entries_are_skipped(self):
        for items in ([],[{**OPEN,'Amount':0}],[{**OPEN,'Amount':-1}]):
            api=self.api(items=items)
            result=await c.change_selected(api,SELECTION,ACCOUNTS,Audit())
            self.assertEqual(result['state'],'skipped');api.change_customer.assert_not_awaited()

    async def test_wrong_identity_debtor_or_entry_type_is_not_written(self):
        for changes in ({'YourRef':'TD11111'},{'Customer':ACCOUNTS['109372']},{'Status':50},{'Type':21},{'Reversal':True}):
            api=self.api({**HEADER,**changes})
            result=await c.change_selected(api,SELECTION,ACCOUNTS,Audit())
            self.assertEqual(result['state'],'review');api.change_customer.assert_not_awaited()

    async def test_ambiguous_open_item_is_not_written(self):
        api=self.api(items=[OPEN,OPEN])
        result=await c.change_selected(api,SELECTION,ACCOUNTS,Audit())
        self.assertEqual(result['state'],'review');api.change_customer.assert_not_awaited()

    async def test_persistence_failure_blocks_write(self):
        api=self.api();audit=Audit()
        audit.persist_event=lambda event: (_ for _ in ()).throw(RuntimeError('storage unavailable'))
        with self.assertRaises(RuntimeError):await c.change_selected(api,SELECTION,ACCOUNTS,audit)
        api.change_customer.assert_not_awaited()

    async def test_ambiguous_put_has_one_attempt_and_no_completion(self):
        api=self.api();audit=Audit();api.change_customer.side_effect=m.Stop('transport failure')
        with self.assertRaises(m.Stop):await c.change_selected(api,SELECTION,ACCOUNTS,audit)
        self.assertEqual(api.change_customer.await_count,1)
        self.assertEqual([e['event'] for e in audit.events],['write_intent'])

    async def test_budget_reserve_stops_before_reads_and_write(self):
        api=self.api();api.limits={'remaining':c.DAILY_RESERVE+2}
        result=await c.change_selected(api,SELECTION,ACCOUNTS,Audit())
        self.assertEqual(result['state'],'pending');api.rows.assert_not_awaited();api.change_customer.assert_not_awaited()

    async def test_accounts_resolved_once_without_payment_condition_reads(self):
        api=AsyncMock();api.rows.return_value=[{'ID':guid,'Code':code.rjust(18),'IsSales':True,'Status':'C'} for code,guid in ACCOUNTS.items()]
        with patch.object(c,'_accounts',None):
            self.assertEqual(await c.route_accounts(api),ACCOUNTS)
            self.assertEqual(await c.route_accounts(api),ACCOUNTS)
        api.rows.assert_awaited_once()
        self.assertEqual(api.rows.await_args.args[0],'crm/Accounts')
