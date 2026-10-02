import unittest
import httpx
from woo_read import configuration, get_orders, read_window, ReadError

class ReaderTests(unittest.TestCase):
    def test_configuration_rejects_unsafe_destinations(self):
        for url in ['http://shop.example', 'https://user:pass@shop.example', 'https://127.0.0.1', 'https://localhost', 'https://shop.example?token=x']:
            with self.subTest(url=url), self.assertRaises(ReadError):
                configuration({'WOO_BASE_URL':url,'WOO_CONSUMER_KEY':'ck_test','WOO_CONSUMER_SECRET':'cs_test'})

    def test_two_pages_only_get_and_minimal_fields(self):
        calls=[]
        def handler(req):
            calls.append(req)
            page=int(req.url.params['page'])
            n=100 if page==1 else 1
            return httpx.Response(200,headers={'X-WP-Total':'101','X-WP-TotalPages':'2'},json=[{'id':(page-1)*100+i,'number':str((page-1)*100+i),'billing':{'email':'not-retained'}} for i in range(1,n+1)])
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            rows=read_window(c,'https://shop.example/orders','2026-09-01','2026-10-01')
        self.assertEqual(len(rows),101)
        self.assertTrue(all(r.method=='GET' for r in calls))
        self.assertNotIn('billing',str(rows))

    def test_redirect_errors_hide_body_and_do_not_follow(self):
        seen=[]
        def handler(req):
            seen.append(req)
            return httpx.Response(302,headers={'Location':'https://other.example'},text='SECRET')
        with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False) as c:
            with self.assertRaises(ReadError) as e: get_orders(c,'https://shop.example/orders',{})
        self.assertNotIn('SECRET',str(e.exception))
        self.assertEqual(len(seen),1)

    def test_missing_headers_and_incomplete_results_fail(self):
        for headers in [{},{'X-WP-Total':'2','X-WP-TotalPages':'1'}]:
            with self.subTest(headers=headers):
                with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers=headers,json=[{'id':1}])) ) as c:
                    with self.assertRaises(ReadError): read_window(c,'https://shop.example/orders','2026-09-01','2026-10-01')

    def test_page_limit_fails_without_returning_partial_orders(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'X-WP-Total':'3000','X-WP-TotalPages':'30'},json=[{'id':1}]))) as c:
            with self.assertRaises(ReadError): read_window(c,'https://shop.example/orders','2026-09-01','2026-10-01')

if __name__=='__main__': unittest.main()
