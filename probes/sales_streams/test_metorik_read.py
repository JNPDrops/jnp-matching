import unittest
import httpx
from metorik_read import get_json, store_info, sample, report, ReadError

class MetorikTests(unittest.TestCase):
    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def test_store_is_allowlisted_and_platform_checked(self):
        data=dict(name='Demo',timezone='Europe/Amsterdam',currency='EUR',platform='woocommerce',earliest_date='2025-01-01',secret='hidden')
        with self.client(lambda r:httpx.Response(200,json=data)) as c:
            self.assertNotIn('secret',store_info(c))
        data['platform']='shopify'
        with self.client(lambda r:httpx.Response(200,json=data)) as c:
            with self.assertRaises(ReadError): store_info(c)

    def test_bounded_sample_no_pii_and_get_only(self):
        calls=[]
        def handler(r):
            calls.append(r)
            return httpx.Response(200,json={'data':[{'order_id':1,'billing_address_email':'hidden'}], 'pagination':{'current_page':1,'per_page':1,'has_more_pages':True}})
        with self.client(handler) as c:
            rows, more=sample(c,1,pause=lambda _:None)
        self.assertTrue(more)
        self.assertNotIn('hidden',str(rows))
        self.assertEqual(calls[0].method,'GET')
        self.assertEqual(calls[0].url.host,'app.metorik.com')

    def test_missing_pagination_fails(self):
        with self.client(lambda r:httpx.Response(200,json={'data':[]})) as c:
            with self.assertRaises(ReadError): sample(c,pause=lambda _:None)

    def test_redirect_and_error_body_not_exposed(self):
        for code in (202,302,401,429):
            with self.subTest(code=code), self.client(lambda r:httpx.Response(code,text='secret-value')) as c:
                with self.assertRaises(ReadError) as e: get_json(c,'')
                self.assertNotIn('secret-value',str(e.exception))

    def test_duplicate_ids_fail(self):
        with self.client(lambda r:httpx.Response(200,json={'data':[{'order_id':1},{'order_id':1}], 'pagination':{'current_page':1,'per_page':2,'has_more_pages':False}})) as c:
            with self.assertRaises(ReadError): sample(c,2,pause=lambda _:None)

    def test_refund_summary_is_not_a_credit_record(self):
        rows=[dict(order_id=123,order_number='123',payment_method='demo',status='completed',currency='EUR',total='20',total_refunds='5')]
        r=report(rows,{'demo':dict(stream='demo',debtor_code='DEMO',confirmed=True)}, {},False)
        o=r['orders'][0]
        self.assertEqual(o['routing_status'],'REVIEW')
        self.assertIn('REFUND_REQUIRES_CREDIT_REVIEW',o['issues'])
        self.assertIn('REFUND_INFORMATION_MISSING',o['issues'])
        self.assertFalse(o['exact_write_allowed'])
        self.assertNotIn('order_id',o)

if __name__=='__main__': unittest.main()
