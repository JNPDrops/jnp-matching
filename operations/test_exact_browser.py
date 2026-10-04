"""Offline tests. They never start a browser, contact Exact, or use real secrets."""
import itertools
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from operations import exact_browser as b, exact_login_probe as p

TEST_ENV = {'EXACT_WEB_USERNAME': 'test@example.invalid', 'EXACT_WEB_PASSWORD': 'fixture-password',
            'EXACT_WEB_TOTP_SECRET': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}


def element(*, text='', action=b.TARGET, visible=True, enabled=True):
    item = MagicMock()
    item.is_visible = AsyncMock(return_value=visible)
    item.is_enabled = AsyncMock(return_value=enabled)
    item.inner_text = AsyncMock(return_value=text)
    item.evaluate = AsyncMock(return_value=action)
    item.fill = AsyncMock()
    item.click = AsyncMock()
    item.count = AsyncMock(return_value=1)
    item.get_attribute = AsyncMock(return_value=None)
    return item


def collection(items):
    result = MagicMock()
    result.all = AsyncMock(return_value=items)
    return result


class ExactConfigurationTests(unittest.TestCase):
    def test_credentials_are_separate_from_oauth_and_never_printed(self):
        credentials = b.Credentials.from_env(TEST_ENV)
        self.assertEqual(credentials.totp.code(59), '287082')
        for value in TEST_ENV.values():
            self.assertNotIn(value, repr(credentials))
        with self.assertRaises(b.LoginStopped):
            b.Credentials.from_env({'EXACT_CLIENT_ID': 'oauth-id', 'EXACT_CLIENT_SECRET': 'oauth-secret'})

    def test_invalid_totp_has_no_value_in_error(self):
        with self.assertRaises(b.LoginStopped) as error:
            b.Credentials.from_env({**TEST_ENV, 'EXACT_WEB_TOTP_SECRET': 'private-invalid-value'})
        self.assertEqual(str(error.exception), 'invalid_configuration')

    def test_exact_hosts_only_and_division_is_unambiguous(self):
        for url in ['http://start.exactonline.nl/', 'https://login.exact.com.evil.test/',
                    'https://start.exactonline.nl@evil.test/', 'https://x@start.exactonline.nl/',
                    'https://login.exact.com:8443/', 'https://login.exact.com:bad/']:
            self.assertFalse(b.trusted(url), url)
        self.assertTrue(b.trusted('https://login.exact.com/a/b'))
        self.assertTrue(b.target_page(b.TARGET))
        self.assertFalse(b.target_page(b.TARGET + '&_Division_=123'))
        self.assertFalse(b.target_page(b.TARGET.replace(b.DIVISION, '123')))

    def test_status_cannot_contain_secrets_or_claim_financial_writes(self):
        result = b.safe_result('failed', 'password', password_submitted='fixture-password',
                               missing=['EXACT_WEB_PASSWORD', 'fixture-password'])
        self.assertFalse(result['password_submitted'])
        self.assertFalse(result['financial_writes'])
        self.assertNotIn('fixture-password', json.dumps(result))
        with self.assertRaises(ValueError):
            b.safe_result('failed', 'password', reason='raw private error')

    def test_only_noop_javascript_form_action_is_supported(self):
        for action in ['javascript:void(0)', 'JavaScript: void(0);']:
            self.assertTrue(b.form_action_allowed(action))
        for action in ['javascript:submit()', 'javascript:fetch("https://evil.test")',
                       'javascript:void(1)', 'data:text/html,x', 'https://evil.test/']:
            self.assertFalse(b.form_action_allowed(action))

    def test_login_probe_is_opt_in_and_expires(self):
        now = datetime(2026, 10, 4, 15, tzinfo=timezone.utc)
        self.assertFalse(p.eligible({}, now))
        self.assertFalse(p.eligible({'EXACT_LOGIN_PROBE_ID': 'other'}, now))
        self.assertTrue(p.eligible({'EXACT_LOGIN_PROBE_ID': p.PROBE_ID}, now))
        self.assertFalse(p.eligible({'EXACT_LOGIN_PROBE_ID': p.PROBE_ID}, p.EXPIRES_AT))


class ExactLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_accounting_autocomplete_field_is_not_a_verification_form(self):
        frame = SimpleNamespace(url=b.TARGET, locator=lambda selector:
            element(text='Accounting dashboard Search') if selector == 'body' else collection([element()]))
        self.assertEqual(await b.locate_otp(frame, after_password=True), [])

    async def test_verified_administration_can_contain_autofill_suppression_field(self):
        main = SimpleNamespace(name='MainWindow', url='https://start.exactonline.nl/docs/FinMenu.aspx',
            title=AsyncMock(return_value='Accounting dashboard'))
        main.locator = lambda selector: (element(text='Accounting dashboard Search') if selector == 'body'
            else collection([element()]) if selector == b.OTP else collection([]))
        page = SimpleNamespace(url=b.TARGET, frames=[main])
        page.locator = lambda selector: element(text='10 James n Parson B.V.' if selector == '#Administration'
            else 'Accounting dashboard')
        self.assertTrue(await b.verify_administration(page))
        main.locator = lambda selector: (element(text='Enter your authenticator verification code') if selector == 'body'
            else collection([element()]) if selector == b.OTP else collection([]))
        self.assertFalse(await b.verify_administration(page))

    async def test_failure_diagnostics_classify_without_disclosing_page_content(self):
        text = 'The verification code you entered is incorrect. Authenticator test@example.invalid fixture-password 123456'
        frame = SimpleNamespace(url='https://login.exact.com/signin', name='', title=AsyncMock(return_value='Sign in'), locator=lambda selector:
            element(text=text) if selector == 'body' else collection([]))
        signals = await b.collect_signals(SimpleNamespace(url=frame.url, frames=[frame]))
        self.assertIn('code_rejected', signals)
        self.assertIn('authenticator_prompt', signals)
        result = b.safe_result('blocked', 'totp', signals=signals + [text])
        for value in ['fixture-password', '123456', 'test@example.invalid']:
            self.assertNotIn(value, json.dumps(result))

    async def test_authenticator_prompt_identifies_uniquely_named_code_field(self):
        code = element()
        frame = SimpleNamespace(locator=lambda selector: collection([]) if selector == b.OTP
            else element(text='Enter the code from your authenticator app') if selector == 'body'
            else collection([code]))
        self.assertEqual(await b.locate_otp(frame, after_password=True), [code])
        self.assertEqual(await b.locate_otp(frame, after_password=False), [])

    async def test_email_code_does_not_trigger_totp_fallback(self):
        frame = SimpleNamespace(locator=lambda selector: collection([]) if selector == b.OTP
            else element(text='Enter the verification code sent by email') if selector == 'body'
            else collection([element()]))
        self.assertEqual(await b.locate_otp(frame, after_password=True), [])

    async def test_ambiguous_authenticator_fields_are_not_filled(self):
        fields = [element(), element()]
        frame = SimpleNamespace(locator=lambda selector: collection([]) if selector == b.OTP
            else element(text='Use your authenticator app') if selector == 'body'
            else collection(fields))
        with self.assertRaises(b.LoginStopped):
            await b.locate_otp(frame, after_password=True)
        for field in fields:
            field.fill.assert_not_awaited()

    async def flow(self, sequence, *, error=None, auto_otp=False, read_race=False):
        state = {'index': 0}
        fields = {name: element() for name in ['username', 'password', 'totp']}
        frame = MagicMock(url='https://login.exact.com/signin')
        frame.locator.side_effect = lambda selector: (collection([fields['totp']])
            if selector == b.OTP and sequence[state['index']] == 'totp'
            else element(text='Enter your authenticator verification code') if selector == 'body'
            else collection([]))
        page = SimpleNamespace(url=b.TARGET, frames=[frame], goto=AsyncMock())
        def current_input(_frame, selector):
            return fields.get(sequence[state['index']]) if selector == {
                'username': b.USERNAME, 'password': b.PASSWORD}.get(sequence[state['index']]) else None
        async def submitted(*_args):
            if error:
                raise RuntimeError(error)
            state['index'] = min(state['index'] + 1, len(sequence) - 1)
        async def otp_filled(_value):
            if auto_otp:
                state['index'] = min(state['index'] + 1, len(sequence) - 1)
        fields['totp'].fill.side_effect = otp_filled
        raced = False
        async def read_guard(_page):
            nonlocal raced
            if read_race and not raced and state['index'] == 1:
                raced = True
                raise RuntimeError('Execution context was destroyed, most likely because of a navigation')
        clock = itertools.count().__next__
        with patch.object(b, 'guard_page', AsyncMock(side_effect=read_guard)), \
             patch.object(b, 'verify_administration', AsyncMock(side_effect=lambda _: sequence[state['index']] == 'success')), \
             patch.object(b, 'one_input', AsyncMock(side_effect=current_input)), \
             patch.object(b, 'check_destination', AsyncMock()), \
             patch.object(b, 'submit', AsyncMock(side_effect=submitted)) as submit:
            result = await b.authenticate(page, b.Credentials.from_env(TEST_ENV), clock=clock, pause=AsyncMock(), timeout=60)
        return result, fields, submit

    async def test_navigation_read_race_does_not_repeat_credential_submission(self):
        result, fields, submit = await self.flow(['username', 'password', 'totp', 'success'], read_race=True)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(submit.await_count, 3)
        fields['username'].fill.assert_awaited_once()
        fields['password'].fill.assert_awaited_once()

    async def test_username_password_totp_and_administration(self):
        result, fields, submit = await self.flow(['username', 'password', 'totp', 'success'])
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['administration_verified'])
        self.assertTrue(result['totp_submitted'])
        self.assertEqual(submit.await_count, 3)
        fields['username'].fill.assert_awaited_once_with(TEST_ENV['EXACT_WEB_USERNAME'])
        fields['password'].fill.assert_awaited_once_with(TEST_ENV['EXACT_WEB_PASSWORD'])
        self.assertEqual(len(fields['totp'].fill.await_args.args[0]), 6)

    async def test_auto_submitting_otp_does_not_click_authenticated_page(self):
        result, _, submit = await self.flow(['username', 'password', 'totp', 'success'], auto_otp=True)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(submit.await_count, 2)

    async def test_repeated_password_is_not_submitted_again(self):
        result, fields, submit = await self.flow(['password', 'password'])
        self.assertEqual(result['reason'], 'repeated_step')
        fields['password'].fill.assert_awaited_once()
        self.assertEqual(submit.await_count, 1)

    async def test_exception_cannot_disclose_credential_or_form_content(self):
        result, _, _ = await self.flow(['password'], error='fill fixture-password failed at secret-url')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('fixture-password', json.dumps(result))
        self.assertNotIn('secret-url', json.dumps(result))

    async def test_credential_destination_guard_rejects_off_origin_action(self):
        field = element(action='https://evil.test/collect')
        with self.assertRaises(b.LoginStopped):
            await b.check_destination(SimpleNamespace(url=b.TARGET), SimpleNamespace(url=b.TARGET), field)
        field.fill.assert_not_awaited()

    async def test_submit_button_cannot_override_form_to_external_host(self):
        field, button = element(), element(action='https://evil.test/collect')
        frame = SimpleNamespace(url=b.TARGET, get_by_role=lambda *_a, **_kw: collection([button]))
        with self.assertRaises(b.LoginStopped):
            await b.submit(SimpleNamespace(url=b.TARGET), frame, field)
        button.click.assert_not_awaited()

    async def test_ajax_form_accepts_noop_action_on_trusted_exact_frame(self):
        field, button = element(action='javascript:void(0);'), element(action='javascript:void(0);')
        frame = SimpleNamespace(url='https://login.exact.com/signin', get_by_role=lambda *_a, **_kw: collection([button]))
        await b.check_destination(SimpleNamespace(url=b.TARGET), frame, field)
        await b.submit(SimpleNamespace(url=b.TARGET), frame, field)
        button.click.assert_awaited_once()

    async def test_captcha_stops_without_submitting(self):
        frame = SimpleNamespace(url=b.TARGET, locator=lambda _: collection([element()]))
        with self.assertRaises(b.LoginStopped) as error:
            await b.guard_page(SimpleNamespace(url=b.TARGET, frames=[frame]))
        self.assertEqual(str(error.exception), 'verification_required')

    async def test_cached_menu_with_expired_login_iframe_is_not_success(self):
        page = MagicMock(url=b.TARGET)
        page.locator.side_effect = lambda selector: element(text='10 James n Parson B.V.')
        page.frames = [SimpleNamespace(name='MainWindow', url='https://start.exactonline.nl/?ReturnUrl=x')]
        self.assertFalse(await b.verify_administration(page))

    async def test_wrong_company_is_not_success(self):
        page = MagicMock(url=b.TARGET)
        page.locator.return_value = element(text='Different B.V.')
        with self.assertRaises(b.LoginStopped) as error:
            await b.verify_administration(page)
        self.assertEqual(str(error.exception), 'wrong_administration')

    async def test_network_guard_blocks_off_origin_navigation_and_posts(self):
        context = SimpleNamespace(route=AsyncMock())
        await b.protect_requests(context)
        guard = context.route.await_args.args[1]
        for method, nav, url, blocked in [('POST', False, 'https://evil.test/', True),
                ('GET', True, 'https://evil.test/', True), ('POST', False, 'https://login.exact.com/path', False),
                ('GET', False, 'https://cdn.example.invalid/style.css', False)]:
            route = SimpleNamespace(request=SimpleNamespace(method=method, resource_type='document', url=url, is_navigation_request=lambda: nav),
                                    abort=AsyncMock(), continue_=AsyncMock())
            await guard(route)
            self.assertEqual(route.abort.await_count, int(blocked))
            self.assertEqual(route.continue_.await_count, int(not blocked))

        route = SimpleNamespace(request=SimpleNamespace(method='GET', resource_type='xhr', url='https://evil.test/collect', is_navigation_request=lambda: False),
                                abort=AsyncMock(), continue_=AsyncMock())
        await guard(route)
        # Read-only localization/assets may use cross-origin XHR; the auth
        # destination checks still cover forms, navigation and POST requests.
        route.abort.assert_not_awaited()
        route.continue_.assert_awaited_once()


class ProbeOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_probe_does_not_read_database_or_start_browser(self):
        with patch.dict(p.os.environ, {}, clear=True), patch.object(p, 'claim_probe') as claim, \
             patch.object(p.asyncio, 'create_subprocess_exec', AsyncMock()) as start:
            await p.run()
        claim.assert_not_called()
        start.assert_not_awaited()

    async def test_missing_credentials_do_not_consume_attempt(self):
        with patch.dict(p.os.environ, {'EXACT_LOGIN_PROBE_ID': p.PROBE_ID}, clear=True), \
             patch.object(p, 'eligible', return_value=True), patch.object(p, 'claim_probe') as claim, \
             patch.object(p, 'publish') as publish:
            await p.run()
        claim.assert_not_called()
        self.assertEqual(publish.call_args.args[0]['reason'], 'missing_credentials')

    async def test_already_claimed_attempt_never_starts_another_login(self):
        environ = {**TEST_ENV, 'EXACT_LOGIN_PROBE_ID': p.PROBE_ID, 'DATABASE_URL': 'private-db'}
        with patch.dict(p.os.environ, environ, clear=True), patch.object(p, 'eligible', return_value=True), \
             patch.object(p, 'claim_probe', return_value=False), patch.object(p, 'publish') as publish, \
             patch.object(p.asyncio, 'create_subprocess_exec', AsyncMock()) as start:
            await p.run()
        start.assert_not_awaited()
        self.assertEqual(publish.call_args.args[0]['status'], 'skipped')

    async def test_child_and_installer_environments_are_minimal(self):
        environ = {**TEST_ENV, 'EXACT_LOGIN_PROBE_ID': p.PROBE_ID, 'DATABASE_URL': 'private-db',
                   'EXACT_CLIENT_SECRET': 'private-oauth', 'PARAGON_PASSWORD': 'private-paragon', 'PATH': '/bin'}
        passed = b.safe_result('passed', 'complete', administration_verified=True)
        passed['password'] = 'child-must-not-publish-this'
        installer = SimpleNamespace(pid=999991, returncode=0, wait=AsyncMock(return_value=0))
        worker = SimpleNamespace(pid=999992, returncode=0, communicate=AsyncMock(return_value=(json.dumps(passed).encode(), b'')))
        with patch.dict(p.os.environ, environ, clear=True), patch.object(p, 'eligible', return_value=True), \
             patch.object(p, 'claim_probe', return_value=True), patch.object(p, 'store_result') as store, \
             patch.object(p, 'publish') as publish, patch.object(p, 'stop_child', AsyncMock()), \
             patch.object(p.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=[installer, worker])) as start:
            await p.run()
        install_env = start.await_args_list[0].kwargs['env']
        worker_env = start.await_args_list[1].kwargs['env']
        self.assertFalse(set(b.ENV_NAMES) & set(install_env))
        for name in ['DATABASE_URL', 'EXACT_CLIENT_SECRET', 'PARAGON_PASSWORD']:
            self.assertNotIn(name, worker_env)
        self.assertEqual(set(b.ENV_NAMES) - set(worker_env), set())
        self.assertNotIn('child-must-not-publish-this', json.dumps(publish.call_args.args[0]))
        self.assertTrue(store.call_args.args[1]['administration_verified'])


if __name__ == '__main__':
    unittest.main()
