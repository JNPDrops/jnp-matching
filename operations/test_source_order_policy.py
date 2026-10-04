import unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI, HTTPException
from operations import source_order_policy as p


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_actions_cannot_reach_financial_handler(self):
        app=FastAPI(); reached=[]
        app.add_middleware(p.SourceOrderPolicyMiddleware)
        @app.post(p.legacy.router.prefix+'/{action}')
        async def legacy(action):
            reached.append(action)
            return {'ok':True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            with patch.object(p.legacy,'authorize'):
                for action in ['automatic','settle_example','automatic/']:
                    result=await client.post(p.legacy.router.prefix+'/'+action)
                    self.assertEqual(result.status_code,409)
                self.assertEqual((await client.post(p.legacy.router.prefix+'/reconcile')).status_code,200)
            with patch.object(p.legacy,'authorize',side_effect=HTTPException(404,'Not found')):
                self.assertEqual((await client.post(p.legacy.router.prefix+'/automatic')).status_code,404)
        self.assertEqual(reached,['reconcile'])

    async def test_audit_flags_source_mismatch_and_ignores_offsets(self):
        common=dict(AccountCode=' test-debtor',EntryID='entry',EntryNumber=1,AmountDC=10)
        rows=[dict(common,ID='a',Description='Order TD10001 | source',YourRef='TD10002'),
              dict(common,ID='b',Description='Order TD10003 | source',YourRef='TD10003'),
              dict(common,ID='c',Description='Order TD10001 | source',YourRef='TD10002',AmountDC=-10),
              dict(common,ID='d',Description='Payment difference',YourRef='TD10002')]
        found=p.candidates(rows,'test-debtor')
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['source_order'],'TD10001')
        self.assertEqual(found[0]['allocated_reference'],'TD10002')


if __name__=='__main__': unittest.main()
