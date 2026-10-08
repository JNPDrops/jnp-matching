import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from operations import icepay_transactions as t

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def recover(self, contents):
        page=AsyncMock(); queue=asyncio.Queue(); links={}
        for i,content in enumerate(contents):
            link=AsyncMock()
            async def click(c=content): await queue.put(c)
            link.click.side_effect=click; links[str(i)]=link
        artifacts={}
        async def identity(value): return value
        with patch.object(t,'open_notifications',AsyncMock()), patch.object(t.asyncio,'sleep',AsyncMock()), patch.object(t,'export_notifications',AsyncMock(return_value=[])), patch.object(t,'download_control_metadata',AsyncMock(return_value=[])), patch.object(t,'csv_links',AsyncMock(return_value=links)), patch.object(t.b,'guard_page',AsyncMock()), patch.object(t,'read_download',side_effect=identity):
            try: result=await t.resume_payment_export(page,queue,['123'],artifacts)
            except t.AcquisitionStopped as e: result=str(e)
        return result,artifacts,links
    async def test_old_notifications_do_not_block_matching_export(self):
        content=b'PaymentID;Amount\n123;42.50\n'
        result,artifacts,links=await self.recover([content]*8)
        self.assertIn(b'123',result)
        self.assertEqual(sum(l.click.await_count for l in links.values()),1)
        self.assertEqual(len(artifacts['candidate_csvs']),1)
    async def test_download_bound_and_missing_identity_fail_closed(self):
        result,artifacts,links=await self.recover([b'PaymentID;Amount\n999;42.50\n']*8)
        self.assertEqual(result,'export_not_completed')
        self.assertEqual(sum(l.click.await_count for l in links.values()),5)
        self.assertEqual(len(artifacts['candidate_csvs']),5)
    async def test_duplicate_identity_rejected(self):
        result,_,_=await self.recover([b'PaymentID;Amount\n123;42.50\n123;42.50\n'])
        self.assertEqual(result,'export_not_completed')

if __name__=='__main__': unittest.main()
