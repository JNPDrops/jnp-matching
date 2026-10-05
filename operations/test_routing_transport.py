import ssl
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace
from operations import bacs_debtor_transfer as m


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_retains_reset_without_reading_body_or_retrying(self):
        app=SimpleNamespace(DIVISION=m.DIVISION,BASE_URL=m.BASE,COLLECTIVE_DEBTOR_CODE=m.SOURCE,DATABASE_URL='')
        app._access_token=AsyncMock(return_value='test-token')
        response=Mock(status_code=429)
        response.headers={'x-ratelimit-remaining':'0','x-ratelimit-reset':'1791064800000'}
        client=AsyncMock();client.__aenter__.return_value=client;client.request.return_value=response
        api=m.Exact(app)
        with patch.object(m.httpx,'AsyncClient',return_value=client),patch.object(m.asyncio,'sleep',AsyncMock()):
            with self.assertRaises(m.ExactRequestError) as caught:
                await api.request('PUT',m.BASE+'/test',payload={'Customer':'test'})
        self.assertEqual(caught.exception.limits,{'remaining':0,'reset_ms':1791064800000})
        client.request.assert_awaited_once();response.json.assert_not_called()

    async def test_requests_share_verified_tls_without_redirect_or_retry(self):
        app=SimpleNamespace(DIVISION=m.DIVISION, BASE_URL=m.BASE, COLLECTIVE_DEBTOR_CODE=m.SOURCE,DATABASE_URL='')
        app._access_token=AsyncMock(return_value='test-token')
        api=m.Exact(app)
        response=Mock(status_code=200,content=b'{}')
        response.headers={}
        response.json.return_value={}
        client=AsyncMock()
        client.__aenter__.return_value=client
        client.request.return_value=response
        with patch.object(m.httpx,'AsyncClient',return_value=client) as factory, \
             patch.object(m.asyncio,'sleep',AsyncMock()):
            await api.request('GET',m.BASE+'/read-only-test')
            await api.request('GET',m.BASE+'/read-only-test')
        self.assertEqual(factory.call_count,2)
        for call in factory.call_args_list:
            self.assertIs(call.kwargs['verify'],m.TLS_CONTEXT)
            self.assertFalse(call.kwargs['follow_redirects'])
            self.assertFalse(call.kwargs['trust_env'])
        self.assertEqual(m.TLS_CONTEXT.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(m.TLS_CONTEXT.check_hostname)
        self.assertGreater(m.TLS_CONTEXT.cert_store_stats()['x509_ca'],0)
