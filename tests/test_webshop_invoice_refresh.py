from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from operations import strict_order_matching as s


class WebshopRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def run_refresh(self, lookup):
        receipt = {'source_order':'TD12345','woo':'123','amount':'10.00',
            'state':'exception_verified','exception':'invoice_missing_or_ambiguous',
            'invoice_refresh':{'at':'2026-10-01T00:00:00Z','rows':[]},'attempts':[],
            'webshop_order':{'status':'processing'}}
        plan = {'receipts':[receipt], 'invoices':[]}
        with patch.object(s,'load',return_value=plan), patch.object(s.legacy,'update'), \
             patch.object(s.legacy,'read_all',new=AsyncMock(return_value=[])) as read, \
             patch.object(s.legacy,'artifact') as save, \
             patch.object(s.legacy,'application',return_value=SimpleNamespace(COLLECTIVE_DEBTOR_CODE='100100')), \
             patch('operations.metorik_bacs_evidence.lookup_orders',lookup):
            await s.refresh_missing(1)
        read.assert_awaited_once()
        self.assertEqual(read.call_args.args[0],'financialtransaction/TransactionLines')
        save.assert_called_once()
        self.assertEqual(receipt['attempts'],[])
        self.assertEqual(receipt['state'],'exception_verified')
        return receipt

    async def test_rechecks_previously_missing_order_and_retains_only_relevant_fields(self):
        lookup = AsyncMock(return_value={'orders':{'#12345':{'order_id':123,'order_number':'#12345',
            'status':'processing','order_updated_at':'2026-10-05T18:00:00Z','customer_email':'excluded@test'}}})
        receipt = await self.run_refresh(lookup)
        self.assertEqual(receipt['webshop_order']['status'],'processing')
        self.assertNotIn('customer_email',receipt['webshop_order'])
        lookup.assert_awaited_once_with(['TD12345'])

    async def test_failed_source_clears_old_order_evidence_without_claiming_absence(self):
        receipt = await self.run_refresh(AsyncMock(side_effect=RuntimeError('private source error')))
        self.assertEqual(receipt['webshop_lookup_state'],'unavailable')
        self.assertIsNone(receipt['webshop_order'])
        self.assertNotIn('private source error',str(receipt))


if __name__=='__main__':
    unittest.main()
