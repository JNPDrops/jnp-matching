"""Immediate historical routes, permanent app assignment, no quota key switching."""
from datetime import timedelta
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import automatic_debtor_routing as a, debtor_routing_policy as p
from operations import allocation_connection as allocation, customer_only_routing as c
from operations import bacs_debtor_transfer as m
from operations.test_debtor_routing_policy import ACCOUNTS, ID, ORDER


class IcepayNowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.status_patch=patch.dict(a.STATUS,dict(a.STATUS),clear=True)
        self.status_patch.start()
        self.addCleanup(self.status_patch.stop)

    def test_only_authorized_historical_routes_can_run_early(self):
        before=p.START_AT-timedelta(hours=2)
        for method in p.CLEANUP_ROUTES:
            self.assertEqual(p.work_allowed(method,'cleanup',before),method in ('icepay-ideal','suap_wordpresspayplugin'))
            self.assertFalse(p.work_allowed(method,'continuous',before))
            self.assertTrue(p.work_allowed(method,'cleanup',p.START_AT))
        self.assertEqual(p.routes_for_now(before),{'icepay-ideal':'109419','suap_wordpresspayplugin':'109422'})
        self.assertFalse(p.work_allowed('unknown','cleanup',p.START_AT))

    async def test_process_early_scopes_without_spending_calls_on_other_routes(self):
        for method,scope,allowed in (('icepay-ideal','cleanup',True),
            ('icepay-ideal','continuous',False),('bacs','cleanup',False),
            ('plisio','cleanup',False),('np_payments','cleanup',False),
            ('suap_wordpresspayplugin','cleanup',True),('suap_wordpresspayplugin','continuous',False)):
            conn=MagicMock();api=AsyncMock()
            with patch.object(a.reviewed_suap,'validate_selection'),patch.object(p,'before_scheduled_start',return_value=True),patch.object(c,'route_accounts',AsyncMock(return_value=ACCOUNTS)),patch.object(c,'change_selected',AsyncMock(return_value={'state':'skipped','reason':'No remaining open item'})) as change:
                await a.process_entry(api,conn,ID,'TD48874',{**ORDER,'payment_method':method},work_scope=scope)
            self.assertEqual(change.await_count,1 if allowed else 0)
            if not allowed:
                expected=p.START_AT if method in p.routes(scope) else 'skipped'
                self.assertEqual(conn.execute.call_args.args[1][0],expected)
                self.assertEqual(api.mock_calls,[])

    async def test_write_guard_binds_durable_historical_evidence_to_its_destination(self):
        app=MagicMock(DIVISION=m.DIVISION,BASE_URL=m.BASE,COLLECTIVE_DEBTOR_CODE=m.SOURCE)
        for evidence,target,allowed in ((('cleanup','icepay-ideal'),'109419',True),
            (('cleanup','suap_wordpresspayplugin'),'109422',True),
            (('cleanup','suap_wordpresspayplugin'),'109419',False),
            (('cleanup','icepay-ideal'),'109422',False),
            (('continuous','suap_wordpresspayplugin'),'109422',False),
            (('continuous','icepay-ideal'),'109419',False),
            (('cleanup','plisio'),'109419',False),(None,'109419',False)):
            conn=MagicMock();conn.execute.return_value.fetchone.side_effect=[(True,p.REVISION),evidence]
            api=a.AutomaticExact(app,conn)
            with patch.object(p,'before_scheduled_start',return_value=True),patch.object(c,'route_accounts',AsyncMock(return_value=ACCOUNTS)),patch.object(m.Exact,'change_customer',AsyncMock()) as write:
                if allowed:await api.change_customer(ID,ACCOUNTS[target])
                else:
                    with self.assertRaises(m.WritePaused):await api.change_customer(ID,ACCOUNTS[target])
            self.assertEqual(write.await_count,1 if allowed else 0)

    async def test_transport_always_uses_allocation_even_after_rate_limit(self):
        app=MagicMock(DIVISION=m.DIVISION,BASE_URL=m.BASE,COLLECTIVE_DEBTOR_CODE=m.SOURCE)
        app._access_token=AsyncMock(side_effect=AssertionError('No primary fallback'))
        api=a.AutomaticExact(app,MagicMock())
        response=MagicMock(status_code=429,headers={'x-ratelimit-remaining':'0','x-ratelimit-reset':'1791072000000'})
        client=AsyncMock();client.__aenter__.return_value=client;client.request.return_value=response
        with patch.object(allocation,'routing_access_token',AsyncMock(return_value='allocation-test-token')) as token,patch.object(m.httpx,'AsyncClient',return_value=client),patch.object(m.asyncio,'sleep',AsyncMock()):
            with self.assertRaises(m.ExactRequestError):await api.request('GET',m.BASE+'/api/v1/3977752/crm/Accounts')
            with self.assertRaises(c.BudgetDeferred):await api.request('GET',m.BASE+'/api/v1/3977752/crm/Accounts')
        app._access_token.assert_not_awaited();client.request.assert_awaited_once()
        token.assert_awaited_once_with(app)
        self.assertEqual(client.request.await_args.kwargs['headers']['Authorization'],'Bearer allocation-test-token')

    async def test_refresh_is_saved_under_allocation_namespace_only(self):
        app=MagicMock();conn=MagicMock();conn.__enter__.return_value=conn
        app._db_connect.return_value=conn
        conn.execute.return_value.fetchone.side_effect=[(True,),({'access_token':'expired','refresh_token':'old','expires_at':0},)]
        config={'client_id':'allocation-client','client_secret':'test','redirect_uri':allocation.CALLBACK}
        fresh={'access_token':'new','refresh_token':'rotated','expires_at':int(time.time())+570}
        with patch.object(allocation,'require_configuration',return_value=config),patch.object(allocation,'exchange',AsyncMock(return_value=fresh)):
            self.assertEqual(await allocation.routing_access_token(app),'new')
        writes=[x for x in conn.execute.call_args_list if 'INSERT INTO exact_oauth_tokens' in x.args[0]]
        self.assertEqual(len(writes),1)
        self.assertTrue(writes[0].args[1][0].startswith('exact_allocation:'))
        self.assertFalse(any(len(x.args)>1 and x.args[1]==('exact',) for x in conn.execute.call_args_list))
