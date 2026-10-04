import unittest
from unittest.mock import AsyncMock
from operations import customer_only_routing as c, debtor_routing_policy as p
from operations.test_customer_only_routing import ID, HEADER, OPEN, Audit
from operations.test_debtor_routing_policy import ACCOUNTS

class ApprovedIcepayCodes(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_aliases_change_only_customer_for_current_open_orders(self):
        for method in ('ic','icepay','icepay-ideal'):
            api=AsyncMock();api.limits={'remaining':500}
            api.rows.side_effect=[[{**HEADER,'Customer':ACCOUNTS['100100']}],
                                  [{**OPEN,'AccountId':ACCOUNTS['100100']}]]
            audit=Audit()
            result=await c.change_selected(api,{'entry_id':ID,'reference':HEADER['YourRef'],
                'order_id':137196,'payment_method':method},ACCOUNTS,audit)
            self.assertEqual(result['destination'],'109419')
            self.assertEqual(api.rows.await_count,2)
            api.change_customer.assert_awaited_once_with(ID,ACCOUNTS['109419'])
            self.assertEqual(audit.events[0]['payload'],{'Customer':ACCOUNTS['109419']})

    def test_no_unapproved_expansion(self):
        for method in ('icepay-card','suap_wordpresspayplugin','unknown',None):
            self.assertNotIn(method,p.CONTINUOUS_ROUTES)
        self.assertEqual(p.CONTINUOUS_ROUTES['np_payments'],'109421')
        self.assertNotIn('np_payments',p.ICEPAY_METHODS)
        for method in ('wc_fibonatix','wc_fibonatics'):
            self.assertEqual(p.RETAIN_ON_SOURCE[method],'100100')
