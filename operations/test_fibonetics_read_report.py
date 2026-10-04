import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from operations import fibonetics_read_report as d


class FiboneticsReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_financial_writes_and_unrelated_resources_before_auth(self):
        app=MagicMock(DIVISION=3977752,BASE_URL=d.m.BASE,COLLECTIVE_DEBTOR_CODE='100100')
        api=d.ReadAPI(app)
        for method,url,payload in (
            ('PUT',d.m.BASE+'/api/v1/3977752/salesentry/SalesEntries',{'Customer':'x'}),
            ('POST',d.m.BASE+'/api/v1/3977752/read/financial/ReceivablesList',{}),
            ('GET',d.m.BASE+'/api/v1/3977752/financialtransaction/BankEntries',None),
            ('GET','https://example.org/api/v1/3977752/salesentry/SalesEntries',None)):
            with self.assertRaises(d.m.Stop):await api.request(method,url,payload=payload)
        self.assertEqual(api.calls,0)
        app._access_token.assert_not_called()

    async def test_complete_pages_project_only_requested_order_fields(self):
        reader=d.OrderReader()
        reader.get=AsyncMock(side_effect=[
            {'data':[{'order_id':7,'order_number':'#12345','email':'PRIVATE','status':'completed'}],
             'pagination':{'current_page':1,'per_page':100,'has_more_pages':True}},
            {'data':[{'order_id':8,'order_number':'#12346','email':'PRIVATE'}],
             'pagination':{'current_page':2,'per_page':100,'has_more_pages':False}}])
        await reader.pages(None,[],{7},set())
        self.assertEqual(set(reader.orders),{7})
        self.assertEqual(reader.orders[7]['status'],'completed')
        self.assertNotIn('PRIVATE',str(reader.orders))
        self.assertEqual(reader.get.await_count,2)

    async def test_empty_intermediate_or_duplicate_page_is_not_complete(self):
        reader=d.OrderReader()
        reader.get=AsyncMock(return_value={'data':[],
             'pagination':{'current_page':1,'per_page':100,'has_more_pages':True}})
        with self.assertRaises(d.m.Stop):await reader.pages(None,[],{7},set())

    def test_credit_link_is_explicit_and_descriptions_are_not_exported(self):
        rows=d.project_headers([{'Description':'Order #12345 / Credit #TD76543'},
                                {'Description':'PRIVATE NAME'}])
        self.assertEqual(rows[0]['original_order_ref'],'TD12345')
        self.assertIsNone(rows[1]['original_order_ref'])
        self.assertNotIn('PRIVATE',str(rows))
