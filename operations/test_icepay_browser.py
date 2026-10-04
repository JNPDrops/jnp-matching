"""Offline tests: synthetic credentials, no real browser or network calls."""
import itertools
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from operations import icepay_browser as b, icepay_fetch_probe as p

ENV = {'ICEPAY_WEB_USERNAME':'fixture@example.invalid', 'ICEPAY_WEB_PASSWORD':'fixture-only-password'}
OTP_ENV = {**ENV, 'ICEPAY_WEB_TOTP_SECRET':'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}


def element(text='', action=b.ORIGIN+'/login'):
    item = MagicMock()
    item.is_visible = AsyncMock(return_value=True)
    item.is_enabled = AsyncMock(return_value=True)
    item.inner_text = AsyncMock(return_value=text)
    item.evaluate = AsyncMock(return_value=action)
    item.get_attribute = AsyncMock(return_value=None)
    item.fill = AsyncMock()
    item.click = AsyncMock()
    return item


def collection(items):
    return SimpleNamespace(all=AsyncMock(return_value=items))


class Configuration(unittest.TestCase):
    def test_only_icepay_credentials_are_accepted(self):
        with self.assertRaises(b.Stopped):
            b.Credentials.from_env({'EXACT_WEB_USERNAME':'x', 'EXACT_WEB_PASSWORD':'y'})
        c = b.Credentials.from_env(ENV)
        self.assertIsNone(c.totp)
        for value in ENV.values():
            self.assertNotIn(value, repr(c))

    def test_optional_totp_and_invalid_config_are_sanitized(self):
        self.assertEqual(b.Credentials.from_env(OTP_ENV).totp.code(59), '287082')
        for environ in [{**ENV,'ICEPAY_WEB_TOTP_SECRET':'private-invalid-value'},
                        {**ENV,'ICEPAY_WEB_USERNAME':'not-an-email'}]:
            with self.assertRaises(b.Stopped) as error:
                b.Credentials.from_env(environ)
            self.assertEqual(str(error.exception), 'invalid_configuration')

    def test_host_and_account_are_exact(self):
        self.assertTrue(b.trusted(b.ORIGIN+'/login'))
        self.assertTrue(b.account_page(b.TARGET+'/payments'))
        self.assertFalse(b.account_page(b.TARGET+'0'))
        for url in ['http://portal.icepay.com/', 'https://portal.icepay.com.evil.test/',
                    'https://portal.icepay.com@evil.test/', 'https://x@portal.icepay.com/',
                    'https://portal.icepay.com:8443/', 'https://portal.icepay.com/#fragment']:
            self.assertFalse(b.trusted(url))

    def test_activation_is_exact_and_expiring(self):
        now = datetime(2026,10,4,tzinfo=timezone.utc)
        self.assertFalse(p.eligible({},now))
        self.assertFalse(p.eligible({p.ACTIVATION:'other'},now))
        self.assertTrue(p.eligible({p.ACTIVATION:p.PROBE_ID},now))
        self.assertFalse(p.eligible({p.ACTIVATION:p.PROBE_ID},p.EXPIRES))

    def test_subprocesses_receive_minimum_environment(self):
        base, child = p.environments({**OTP_ENV,'PATH':'/bin','DATABASE_URL':'private-db',
             'EXACT_WEB_PASSWORD':'private-exact','OTHER_SECRET':'private-other'})
        for name in OTP_ENV:
            self.assertNotIn(name, base)
            self.assertIn(name, child)
        for name in ('DATABASE_URL','EXACT_WEB_PASSWORD','OTHER_SECRET'):
            self.assertNotIn(name, base)
            self.assertNotIn(name, child)

    def test_result_has_no_arbitrary_fields_or_claimed_downloads(self):
        raw = {**b.safe_result('passed','complete'), 'password':'private-value',
               'transactions_downloaded':True,'financial_writes':True,
               'missing':['ICEPAY_WEB_PASSWORD','private-value']}
        result = b.validate_result(raw)
        self.assertNotIn('private-value',json.dumps(result))
        self.assertFalse(result['transactions_downloaded'])
        self.assertFalse(result['financial_writes'])
        with self.assertRaises(ValueError):
            b.safe_result('failed','login_form',reason='raw private message')

    def test_form_storage_rejects_values_html_and_other_accounts(self):
        valid = {'payments':{'path':'/merchant/88292/payments','controls':[
            {'tag':'input','type':'date','id':'from','label':'From'}]}}
        self.assertEqual(p.validate_forms(valid),valid)
        for metadata in [
            {'payments':{'path':'/merchant/88293/payments','controls':[]}},
            {'payments':{'path':'/merchant/88292/payments','controls':[{'value':'private-value'}]}},
            {'payments':{'path':'/merchant/88292/payments','controls':[{'id':'email','date_preview':'private-value'}]}},
            {'payments':{'path':'/merchant/88292/payments','controls':[],'html':'private'}},
            {'cookies':{}}]:
            with self.assertRaises(ValueError):
                p.validate_forms(metadata)

    def test_diagnostics_only_allow_fixed_predicates_and_bounded_counts(self):
        result = b.safe_result('blocked','payments',account_verified=True,
            password_submitted=True, diagnostics={'company_present':True,
            'payments_links':2,'statements_links':'private-value','url':'private-url',
            'login_fields_visible':'private-password'})
        self.assertEqual(result['diagnostics'],{'company_present':True,'payments_links':2})
        self.assertTrue(b.validate_result(result)['account_verified'])
        self.assertTrue(b.validate_result(result)['password_submitted'])

    def test_worker_roundtrip_accepts_empty_options_and_retains_login(self):
        raw = {'result':b.safe_result('passed','complete',account_verified=True,
                 password_submitted=True,pages=['payments']),
               'forms':{'payments':{'path':'/merchant/88292/payments','controls':[
                 {'tag':'button','label':'Export','options':[]}]}}}
        result, forms = p.decode_worker_output(json.dumps(raw).encode())
        self.assertEqual(result['status'],'passed')
        self.assertEqual(forms,raw['forms'])
        # JavaScript undefined becomes Python None, then JSON null. It must not
        # silently turn a proven login into an unexplained runtime failure.
        raw['forms']['payments']['controls'][0]['options'] = None
        result, forms = p.decode_worker_output(json.dumps(raw).encode())
        self.assertEqual(result['reason'],'invalid_form_metadata')
        self.assertTrue(result['account_verified'])
        self.assertTrue(result['password_submitted'])
        self.assertEqual(forms,{})

    def test_worker_bad_payload_never_echoes_output(self):
        for payload in [b'private-worker-error',b'[]',b'{"password":"private"}',b'x'*300001]:
            result, forms = p.decode_worker_output(payload)
            self.assertEqual(result['reason'],'invalid_worker_output')
            self.assertNotIn('private',json.dumps(result))
            self.assertEqual(forms,{})


class Login(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_export_control_is_never_clicked(self):
        buttons = [element(),element()]
        page = SimpleNamespace(get_by_role=lambda *_a,**_k:collection(buttons))
        with self.assertRaises(b.Stopped):
            await b.click_unique_read_control(page,'Export')
        for button in buttons:
            button.click.assert_not_awaited()

    async def test_missing_optional_export_is_not_an_error(self):
        page = SimpleNamespace(get_by_role=lambda *_a,**_k:collection([]))
        self.assertFalse(await b.click_unique_read_control(page,'Export',optional=True))

    async def test_duplicate_navigation_and_breadcrumb_accept_same_target_only(self):
        first, second = element(), element()
        first.get_attribute.return_value = b.TARGET + '/payments'
        second.get_attribute.return_value = b.TARGET + '/payments'
        page = SimpleNamespace(url=b.TARGET,get_by_role=lambda *_a,**_k:collection([first,second]))
        self.assertIs(await b.unique_account_link(page,'Payments'),first)
        for bad in [b.TARGET+'/other','https://evil.test/',b.ORIGIN+'/merchant/88293/payments']:
            second.get_attribute.return_value = bad
            self.assertIsNone(await b.unique_account_link(page,'Payments'))

    async def test_navigation_settles_without_resubmitting_login(self):
        with patch.object(b,'guard_page',AsyncMock()), \
             patch.object(b,'verify_account',AsyncMock(side_effect=[False,False,True])), \
             patch.object(b,'submit',AsyncMock()) as submit:
            await b.wait_verified_account(SimpleNamespace(),clock=itertools.count().__next__,pause=AsyncMock())
            submit.assert_not_awaited()

    async def test_waiting_never_ignores_account_or_human_verification_blocks(self):
        for reason in ['wrong_account','verification_required','credentials_rejected']:
            with patch.object(b,'guard_page',AsyncMock(side_effect=b.Stopped(reason))), \
                 patch.object(b,'verify_account',AsyncMock()) as verify:
                with self.assertRaises(b.Stopped) as error:
                    await b.wait_verified_account(SimpleNamespace(),pause=AsyncMock())
                self.assertEqual(str(error.exception),reason)
                verify.assert_not_awaited()

    async def flow(self, sequence, *, totp=False, auto_otp=False, error=None, read_race=False):
        state = {'index':0}
        fields = {n:element() for n in ('email','password','totp')}
        def current():
            return sequence[state['index']]
        page = SimpleNamespace(url=b.ORIGIN+'/login',goto=AsyncMock(),
            locator=lambda selector:collection([fields['totp']]) if selector==b.OTP and current()=='totp' else collection([]))
        async def input_field(_page, selector):
            return fields['email' if selector==b.EMAIL else 'password'] if current()=='password' else None
        async def submit(*_):
            if error:
                raise RuntimeError(error)
            state['index'] = min(state['index']+1,len(sequence)-1)
        async def fill_otp(_value):
            if auto_otp:
                state['index'] = min(state['index']+1,len(sequence)-1)
        fields['totp'].fill.side_effect = fill_otp
        raced = False
        async def guard(_):
            nonlocal raced
            if read_race and not raced and state['index']==1:
                raced = True
                raise RuntimeError('Execution context was destroyed')
            return 'Authenticator two-factor code' if current()=='totp' else ''
        with patch.object(b,'guard_page',AsyncMock(side_effect=guard)), \
             patch.object(b,'verify_account',AsyncMock(side_effect=lambda _:current()=='success')), \
             patch.object(b,'one_field',AsyncMock(side_effect=input_field)), \
             patch.object(b,'check_form',AsyncMock()), \
             patch.object(b,'submit',AsyncMock(side_effect=submit)) as submitted:
            result = await b.authenticate(page,b.Credentials.from_env(OTP_ENV if totp else ENV),
                clock=itertools.count().__next__,pause=AsyncMock(),timeout=60)
        return result,fields,submitted

    async def test_password_login_without_totp(self):
        result, fields, submit = await self.flow(['password','success'])
        self.assertTrue(result['account_verified'])
        fields['email'].fill.assert_awaited_once_with(ENV['ICEPAY_WEB_USERNAME'])
        fields['password'].fill.assert_awaited_once_with(ENV['ICEPAY_WEB_PASSWORD'])
        self.assertEqual(submit.await_count,1)

    async def test_password_and_totp_with_navigation_race(self):
        result,fields,submit = await self.flow(['password','totp','success'],totp=True,read_race=True)
        self.assertTrue(result['account_verified'])
        self.assertTrue(result['totp_submitted'])
        self.assertEqual(submit.await_count,2)
        fields['password'].fill.assert_awaited_once()
        self.assertEqual(len(fields['totp'].fill.await_args.args[0]),6)

    async def test_auto_submitted_totp_does_not_click_new_page(self):
        result, _, submit = await self.flow(['password','totp','success'],totp=True,auto_otp=True)
        self.assertTrue(result['account_verified'])
        self.assertEqual(submit.await_count,1)

    async def test_totp_requirement_returns_only_missing_variable(self):
        result,fields,_ = await self.flow(['password','totp'])
        self.assertEqual(result['missing'],['ICEPAY_WEB_TOTP_SECRET'])
        fields['totp'].fill.assert_not_awaited()

    async def test_password_is_never_resubmitted(self):
        result,fields,submit = await self.flow(['password','password'])
        self.assertEqual(result['reason'],'repeated_step')
        self.assertEqual(submit.await_count,1)
        fields['password'].fill.assert_awaited_once()

    async def test_browser_error_does_not_disclose_secret(self):
        result,_,_ = await self.flow(['password'],error='fill private-password failed')
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('private-password',json.dumps(result))

    async def test_off_origin_credential_form_is_rejected(self):
        field = element(action='https://evil.test/collect')
        with self.assertRaises(b.Stopped):
            await b.check_form(SimpleNamespace(url=b.ORIGIN+'/login'),field)
        field.fill.assert_not_awaited()

    async def test_submit_override_cannot_exfiltrate_credentials(self):
        button = element(action='https://evil.test/collect')
        page = SimpleNamespace(get_by_role=lambda *_a,**_k:collection([button]))
        with self.assertRaises(b.Stopped):
            await b.submit(page,b.SIGN_IN)
        button.click.assert_not_awaited()

    async def test_captcha_stops(self):
        frame = SimpleNamespace(locator=lambda _:collection([element()]))
        page = SimpleNamespace(url=b.ORIGIN+'/login',frames=[frame])
        with self.assertRaises(b.Stopped) as error:
            await b.guard_page(page)
        self.assertEqual(str(error.exception),'verification_required')

    async def test_other_account_stops_before_reading_page(self):
        with self.assertRaises(b.Stopped) as error:
            await b.guard_page(SimpleNamespace(url=b.ORIGIN+'/merchant/88293'))
        self.assertEqual(str(error.exception),'wrong_account')

    async def test_wrong_company_cannot_count_as_login(self):
        page = SimpleNamespace(url=b.TARGET,locator=lambda s:
            element('Other Company B.V.') if s=='body' else collection([]))
        with self.assertRaises(b.Stopped) as error:
            await b.verify_account(page)
        self.assertEqual(str(error.exception),'wrong_account')

    async def test_network_guard_stops_off_origin_posts_and_navigation(self):
        context = SimpleNamespace(route=AsyncMock())
        await b.protect_requests(context)
        guard = context.route.await_args.args[1]
        for method,nav,url,blocked in [('POST',False,'https://evil.test',True),
                ('GET',True,'https://evil.test',True),('POST',False,b.ORIGIN+'/livewire/update',False),
                ('GET',False,'https://cdn.example.invalid/style.css',False)]:
            route = SimpleNamespace(request=SimpleNamespace(method=method,url=url,is_navigation_request=lambda:nav),
                                    abort=AsyncMock(),continue_=AsyncMock())
            await guard(route)
            self.assertEqual(route.abort.await_count,int(blocked))


class Job(unittest.IsolatedAsyncioTestCase):
    async def test_missing_config_does_not_consume_claim_or_launch(self):
        with patch.dict(p.os.environ,{p.ACTIVATION:p.PROBE_ID},clear=True), \
             patch.object(p,'claim') as claim, patch.object(p,'publish') as publish, \
             patch.object(p.asyncio,'create_subprocess_exec') as launch:
            await p.run()
            claim.assert_not_called()
            launch.assert_not_called()
            self.assertEqual(publish.call_args.args[0]['missing'],list(b.REQUIRED))

    async def test_existing_claim_does_not_repeat_login(self):
        with patch.dict(p.os.environ,{**ENV,p.ACTIVATION:p.PROBE_ID,'DATABASE_URL':'fixture-db'},clear=True), \
             patch.object(p,'claim',return_value=False), patch.object(p,'publish') as publish, \
             patch.object(p.asyncio,'create_subprocess_exec') as launch:
            await p.run()
            launch.assert_not_called()
            self.assertEqual(publish.call_args.args[0]['status'],'skipped')


if __name__ == '__main__':
    unittest.main()
