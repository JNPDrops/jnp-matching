"""The optional second connection may only authenticate and probe quota."""
import asyncio
from contextlib import nullcontext
import json
import time
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, Request, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
import httpx

from operations import allocation_connection as c

CONFIG={'EXACT_ALLOCATION_CLIENT_ID':'allocation-client','EXACT_ALLOCATION_CLIENT_SECRET':'allocation-test-secret'}
CFG={'client_id':'allocation-client','client_secret':'allocation-test-secret','redirect_uri':c.CALLBACK}


def app_module():
    app=MagicMock(DATABASE_URL='configured',CLIENT_ID='primary-client',SESSION_SECRET='separate-session-secret-for-testing')
    conn=MagicMock();conn.__enter__.return_value=conn
    conn.execute.return_value.fetchone.return_value=(True,)
    app._db_connect.return_value=conn
    return app,conn


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.main,self.conn=app_module()
        self.app=FastAPI();self.app.include_router(c.router)
        self.app.add_middleware(SessionMiddleware,secret_key='session-test-only')
        self.client=TestClient(self.app,base_url='https://testserver')
        self.env=patch.dict(c.os.environ,CONFIG,clear=True);self.env.start()
        self.module=patch.object(c,'main_module',return_value=self.main);self.module.start()

    def tearDown(self):
        self.module.stop();self.env.stop();self.client.close()

    def test_unconfigured_status_does_not_read_any_tokens(self):
        with patch.dict(c.os.environ,{},clear=True):r=self.client.get('/allocation/status')
        self.assertEqual(r.status_code,200)
        self.assertFalse(r.json()['configured']);self.assertFalse(r.json()['connected'])
        self.conn.execute.assert_not_called()

    def test_configuration_requires_separate_credentials_and_safe_callback(self):
        with patch.dict(c.os.environ,{'EXACT_ALLOCATION_REDIRECT_URI':'https://wrong.example/callback'}):
            self.assertEqual(self.client.get('/allocation/login').status_code,503)
        self.main.CLIENT_ID='allocation-client'
        self.assertEqual(self.client.get('/allocation/login').status_code,409)

    def test_authorization_redirect_and_callback_state(self):
        r=self.client.get('/allocation/login',follow_redirects=False)
        parsed=urlparse(r.headers['location']);q=parse_qs(parsed.query)
        self.assertEqual(parsed.netloc,'start.exactonline.nl')
        self.assertEqual(parsed.path,'/api/oauth2/auth')
        self.assertEqual(q['client_id'],['allocation-client'])
        self.assertEqual(q['redirect_uri'],[c.CALLBACK])
        self.assertNotIn('allocation-test-secret',r.headers['location'])
        with patch.object(c,'exchange',AsyncMock()) as exchange:
            r=self.client.get('/oauth/allocation/callback?code=one-use-code&state=wrong')
        self.assertEqual(r.status_code,400);exchange.assert_not_awaited()
        self.assertNotIn('one-use-code',r.text)

    def test_successful_callback_saves_separate_tokens_and_runs_one_read_probe(self):
        r=self.client.get('/allocation/login',follow_redirects=False)
        state=parse_qs(urlparse(r.headers['location']).query)['state'][0]
        tokens={'access_token':'fake-access','refresh_token':'fake-refresh','expires_at':int(time.time())+500}
        with patch.object(c,'exchange',AsyncMock(return_value=tokens)),patch.object(c,'probe',AsyncMock(return_value={'http_status':200})) as probe:
            r=self.client.get('/oauth/allocation/callback',params={'code':'fake-one-use-code','state':state},follow_redirects=False)
        self.assertEqual(r.status_code,303);self.assertEqual(r.headers['location'],'/allocation/status')
        probe.assert_awaited_once()
        saves=[x for x in self.conn.execute.call_args_list if 'INSERT INTO exact_oauth_tokens' in x.args[0]]
        self.assertEqual(len(saves),1)
        self.assertTrue(saves[0].args[1][0].startswith('exact_allocation:'))
        self.assertNotEqual(saves[0].args[1][0],'exact')
        self.assertEqual(json.loads(saves[0].args[1][1])['access_token'],'fake-access')
        # A used state cannot exchange the code again.
        with patch.object(c,'exchange',AsyncMock()) as exchange:
            r=self.client.get('/oauth/allocation/callback',params={'code':'fake-one-use-code','state':state})
        self.assertEqual(r.status_code,400);exchange.assert_not_awaited()

    def test_status_reads_only_cached_metadata_and_never_probes(self):
        self.conn.execute.return_value.fetchone.return_value=({'daily_remaining':4321},)
        with patch.object(c,'probe',AsyncMock()) as probe:
            r=self.client.get('/allocation/status')
        self.assertEqual(r.json()['last_probe'],{'daily_remaining':4321})
        self.assertTrue(r.json()['connected']);probe.assert_not_awaited()
        self.assertIn("SELECT token_json->'probe'",self.conn.execute.call_args.args[0])
        self.assertNotIn('allocation-test-secret',r.text)

    def test_public_request_cannot_spend_exact_calls(self):
        with patch.object(c,'probe',AsyncMock()) as probe:
            r=self.client.post('/allocation/probe',data={'csrf':'fake'})
        self.assertEqual(r.status_code,403);probe.assert_not_awaited()
        self.conn.execute.assert_not_called()

    def test_token_namespace_depends_on_client_id(self):
        self.assertNotEqual(c.store_key(CFG),c.store_key({**CFG,'client_id':'another-client'}))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_probe_uses_one_get_and_never_reads_or_returns_body(self):
        conn=MagicMock()
        tokens={'access_token':'fake-access','refresh_token':'fake-refresh','expires_at':int(time.time())+500}
        response=MagicMock(status_code=200,headers={
            'x-ratelimit-limit':'5000','x-ratelimit-remaining':'4999','x-ratelimit-reset':'1791072000000',
            'x-ratelimit-minutely-limit':'60','x-ratelimit-minutely-remaining':'59'})
        response.json.side_effect=AssertionError('Response body must never be read')
        client=AsyncMock();client.get.return_value=response
        with patch.object(c.httpx,'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            result=await c.probe(conn,CFG,tokens)
            second=await c.probe(conn,CFG,tokens)
        self.assertEqual(result,second);client.get.assert_awaited_once();client.post.assert_not_awaited()
        self.assertIn('/api/v1/3977752/crm/Accounts',client.get.await_args.args[0])
        self.assertEqual(result['daily_remaining'],4999)
        self.assertIsNone(result['administration_remaining'])
        self.assertNotIn('fake-access',json.dumps(result))
        self.assertNotIn('fake-refresh',json.dumps(result))
        response.json.assert_not_called()

    async def test_token_refresh_is_persisted_before_read(self):
        conn=MagicMock();events=[]
        conn.execute.side_effect=lambda *args:events.append('save')
        old={'access_token':'old','refresh_token':'old-refresh','expires_at':0}
        new={'access_token':'new','refresh_token':'new-refresh','expires_at':int(time.time())+500}
        client=AsyncMock()
        async def get(*args,**kwargs):
            events.append('get')
            self.assertEqual(kwargs['headers']['Authorization'],'Bearer new')
            return httpx.Response(429,headers={'x-ratelimit-remaining':'0'})
        client.get.side_effect=get
        with patch.object(c,'exchange',AsyncMock(return_value=new)),patch.object(c.httpx,'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            result=await c.probe(conn,CFG,old)
        self.assertEqual(events,['save','get','save'])
        self.assertTrue(result['rate_limited']);self.assertFalse(result['read_succeeded'])

    async def test_exchange_errors_never_return_credentials_or_response_body(self):
        client=AsyncMock();client.post.return_value=httpx.Response(400,text='secret-error-response')
        with patch.object(c.httpx,'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            with self.assertRaises(HTTPException) as error:await c.exchange(CFG,{'grant_type':'authorization_code','code':'fake-code'})
        self.assertNotIn('secret-error-response',str(error.exception))
        self.assertNotIn(CFG['client_secret'],str(error.exception))

    def test_missing_headers_are_unknown_not_assumed_available(self):
        result=c.quota_result(httpx.Response(403))
        self.assertIsNone(result['daily_remaining'])
        self.assertFalse(result['read_succeeded'])
