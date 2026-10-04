"""The requested diagnosis cannot write or disclose unrestricted source fields."""
from contextlib import nullcontext
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from operations import requested_route_diagnostic as d


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_reads_and_projects_operational_fields(self):
        app = MagicMock(DIVISION=d.m.DIVISION, BASE_URL=d.m.BASE,
                        COLLECTIVE_DEBTOR_CODE=d.m.SOURCE)
        conn = app._db_connect.return_value.__enter__.return_value
        conn.transaction.return_value = nullcontext()
        conn.execute.return_value.fetchone.return_value = (True,None,None,None,'revision')
        conn.execute.return_value.fetchall.return_value = []
        api = MagicMock(limits={'remaining':4900})
        api.rows = AsyncMock(return_value=[{'YourRef':d.REFERENCE,'Description':'PRIVATE'}])
        proof = {'orders':{'#43893':{'order_id':1,'order_number':'#43893',
                 'payment_method':'np_payments','email':'PRIVATE','total':153}}}
        with patch.object(d,'ReadOnlyExact',return_value=api), \
             patch.object(d.evidence,'lookup_orders',AsyncMock(return_value=proof)), \
             patch.object(d.runtime,'event') as log:
            await d.collect(app)
        self.assertTrue(all(c.args[0].lstrip().startswith(('SELECT','SET TRANSACTION READ ONLY'))
                            for c in conn.execute.call_args_list))
        self.assertEqual(api.rows.await_count,1)
        self.assertEqual(api.rows.await_args.args[1]['$filter'],"YourRef eq 'TD43893'")
        self.assertNotIn('PRIVATE',str(log.call_args_list))
        self.assertEqual(log.call_args.kwargs['cleanup_destination'],'109421')
        self.assertIsNone(log.call_args.kwargs['continuous_destination'])
        api.change_customer.assert_not_called()

    async def test_transport_rejects_writes_before_token_access(self):
        app = MagicMock(DIVISION=d.m.DIVISION, BASE_URL=d.m.BASE,
                        COLLECTIVE_DEBTOR_CODE=d.m.SOURCE)
        api = d.ReadOnlyExact(app)
        for method in ('PUT','POST','DELETE'):
            with self.assertRaises(d.m.Stop):
                await api.request(method,'https://example.invalid',payload={})
        app._access_token.assert_not_called()

    async def test_expiry_and_failures_do_not_break_routing(self):
        with patch.object(d,'collect',AsyncMock(side_effect=RuntimeError('SECRET'))) as collect, \
             patch.object(d.runtime,'event') as log:
            await d.run(None,now=d.EXPIRES)
            collect.assert_not_awaited()
            await d.run(None,now=datetime(2026,10,4,tzinfo=timezone.utc))
            self.assertNotIn('SECRET',str(log.call_args_list))
