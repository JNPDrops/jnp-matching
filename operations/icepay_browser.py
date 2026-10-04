"""ICEPAY login and read-only export-form discovery inside Render.

The login selectors were observed in the merchant portal on 4 October 2026.
Export controls still require live inspection: this module does not invent an
endpoint, submit a payment/refund, or treat an unfiltered export as complete.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import re
import time
from urllib.parse import urljoin, urlsplit

from operations.paragon_login_probe import parse_totp

ORIGIN = 'https://portal.icepay.com'
ACCOUNT = '88292'
MERCHANT = '34950'
TARGET = ORIGIN + '/merchant/' + ACCOUNT
REQUIRED = ('ICEPAY_WEB_USERNAME', 'ICEPAY_WEB_PASSWORD')
OPTIONAL = ('ICEPAY_WEB_TOTP_SECRET',)
ENV_NAMES = REQUIRED + OPTIONAL
EMAIL = 'input[id="form.email"], input[type="email"]'
PASSWORD = 'input[id="form.password"], input[type="password"]'
OTP = 'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[name="token"]'
SIGN_IN = re.compile(r'^(Sign in|Log in|Login|Inloggen|Aanmelden)$', re.I)
VERIFY = re.compile(r'^(Verify|Confirm|Continue|Sign in|Log in|Verifiëren|Bevestigen|Doorgaan|Inloggen)$', re.I)
COMPANY = re.compile(r"James\s*['’]?\s*n\s+Parson\s+B\.?V\.?", re.I)
TOTP_PROMPT = re.compile(r'authenticator|authentication app|authenticatie.?app|two.factor|two.step|twee.?factor|tweestaps', re.I)
CAPTCHA = 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"], .g-recaptcha, .h-captcha'
HUMAN = re.compile(r'verify you are human|checking your browser|unusual traffic|verifieer dat je een mens', re.I)
AUTH_ERROR = re.compile(r'these credentials do not match|incorrect password|invalid password|invalid.{0,20}(code|token)|too many.{0,20}(attempts|requests)|account.{0,25}(locked|blocked)|onjuist.{0,20}(wachtwoord|code)|ongeldig.{0,20}(wachtwoord|code)', re.I)
REASONS = {'none', 'missing_credentials', 'invalid_configuration', 'unexpected_origin',
           'verification_required', 'credentials_rejected', 'unsupported_form',
           'repeated_step', 'timeout', 'wrong_account', 'login_not_verified',
           'export_controls_unverified', 'runtime_error'}
STAGES = {'configuration', 'claim', 'browser_install', 'browser_launch', 'login_form',
          'password', 'totp', 'account', 'payments', 'refunds', 'statements', 'complete', 'runtime'}
STATUSES = {'disabled', 'started', 'passed', 'blocked', 'failed', 'skipped'}


class Stopped(Exception):
    def __init__(self, reason):
        super().__init__(reason if reason in REASONS else 'runtime_error')


def trusted(url):
    try:
        p = urlsplit(url)
        return p.scheme == 'https' and p.netloc == 'portal.icepay.com' and not p.fragment
    except (ValueError, TypeError):
        return False


def account_page(url):
    if not trusted(url):
        return False
    path = urlsplit(url).path.rstrip('/')
    return path == '/merchant/' + ACCOUNT or path.startswith('/merchant/' + ACCOUNT + '/')


def safe_result(status, stage, *, reason='none', password_submitted=False,
                totp_submitted=False, account_verified=False, missing=(), pages=(), diagnostics=None):
    if status not in STATUSES or stage not in STAGES or reason not in REASONS:
        raise ValueError('invalid_result')
    return dict(status=status, stage=stage, reason=reason, fresh_session=True,
                password_submitted=password_submitted is True,
                totp_submitted=totp_submitted is True,
                account_verified=account_verified is True,
                account=ACCOUNT, merchant=MERCHANT, financial_writes=False,
                transactions_downloaded=False,
                missing=[k for k in missing if k in ENV_NAMES],
                pages=sorted({p for p in pages if p in {'payments', 'refunds', 'statements'}}),
                diagnostics=clean_diagnostics(diagnostics))


def clean_diagnostics(raw):
    raw = raw if isinstance(raw, dict) else {}
    allowed = {'trusted_origin', 'account_path', 'company_present',
               'login_fields_visible', 'payments_links', 'statements_links'}
    return {k:v for k,v in raw.items() if k in allowed and
            (type(v) is bool if k not in {'payments_links', 'statements_links'}
             else type(v) is int and 0 <= v <= 20)}


def validate_result(raw):
    allowed = {'status', 'stage', 'reason', 'password_submitted', 'totp_submitted',
               'account_verified', 'missing', 'pages', 'diagnostics'}
    return safe_result(**{k: v for k, v in raw.items() if k in allowed})


@dataclass(repr=False)
class Credentials:
    username: str
    password: str
    totp: object | None = None

    @classmethod
    def from_env(cls, environ):
        if any(not environ.get(n) for n in REQUIRED):
            raise Stopped('missing_credentials')
        username = environ['ICEPAY_WEB_USERNAME'].strip()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', username) or len(username) > 254:
            raise Stopped('invalid_configuration')
        try:
            generator = parse_totp(environ['ICEPAY_WEB_TOTP_SECRET']) if environ.get('ICEPAY_WEB_TOTP_SECRET') else None
            if generator is not None and generator.digits != 6:
                raise ValueError()
        except Exception:
            raise Stopped('invalid_configuration') from None
        return cls(username, environ['ICEPAY_WEB_PASSWORD'], generator)


async def visible(locator):
    return [item for item in await locator.all() if await item.is_visible()]


async def one_field(page, selector):
    fields = [f for f in await visible(page.locator(selector)) if await f.is_enabled()]
    if len(fields) > 1:
        raise Stopped('unsupported_form')
    return fields[0] if fields else None


async def guard_page(page):
    if not trusted(page.url):
        raise Stopped('unexpected_origin')
    if re.match(r'/merchant/\d+', urlsplit(page.url).path) and not account_page(page.url):
        raise Stopped('wrong_account')
    # CAPTCHA iframe origins may differ; stop without entering any values.
    for frame in page.frames:
        if await visible(frame.locator(CAPTCHA)):
            raise Stopped('verification_required')
    body = await page.locator('body').inner_text(timeout=2000)
    if HUMAN.search(body):
        raise Stopped('verification_required')
    if AUTH_ERROR.search(body):
        raise Stopped('credentials_rejected')
    return body


async def check_form(page, field):
    if not trusted(page.url):
        raise Stopped('unexpected_origin')
    action = await field.evaluate('el => el.form ? el.form.action : location.href')
    if not trusted(action):
        raise Stopped('unexpected_origin')


async def submit(page, pattern):
    buttons = [b for b in await visible(page.get_by_role('button', name=pattern)) if await b.is_enabled()]
    if len(buttons) != 1:
        raise Stopped('unsupported_form')
    action = await buttons[0].evaluate("el => el.hasAttribute('formaction') ? el.formAction : (el.form ? el.form.action : location.href)")
    if not trusted(action):
        raise Stopped('unexpected_origin')
    await buttons[0].click()


async def verify_account(page):
    if not account_page(page.url):
        return False
    if await visible(page.locator(EMAIL + ', ' + PASSWORD)):
        return False
    body = await page.locator('body').inner_text(timeout=2000)
    if not COMPANY.search(body):
        raise Stopped('wrong_account')
    for label in ('Payments', 'Statements'):
        if await unique_account_link(page, label) is None:
            return False
    return True


async def unique_account_link(page, label):
    """Sidebar and breadcrumb may repeat a link, but must agree on one target."""
    links = await visible(page.get_by_role('link', name=label, exact=True))
    targets = set()
    for link in links:
        href = await link.get_attribute('href')
        target = urljoin(page.url, href or '')
        if not href or not account_page(target):
            return None
        targets.add(target)
    return links[0] if links and len(targets) == 1 else None


async def wait_verified_account(page, *, timeout=12, clock=time.monotonic, pause=asyncio.sleep):
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            await guard_page(page)
            if await verify_account(page):
                return
        except Stopped:
            raise
        except Exception as error:
            if not transient_read(error):
                raise
        await pause(.4)
    raise Stopped('login_not_verified')


async def account_diagnostics(page):
    """Fixed predicates/counts only; never return URLs, body text or values."""
    result = {'trusted_origin':trusted(page.url), 'account_path':account_page(page.url)}
    if not result['trusted_origin']:
        return result
    try:
        result['company_present'] = bool(COMPANY.search(await page.locator('body').inner_text(timeout=2000)))
        result['login_fields_visible'] = bool(await visible(page.locator(EMAIL + ', ' + PASSWORD)))
        for label in ('Payments', 'Statements'):
            result[label.lower() + '_links'] = min(20,len(await visible(page.get_by_role('link',name=label,exact=True))))
    except Exception:
        pass
    return clean_diagnostics(result)


def transient_read(error):
    return type(error).__name__ == 'TimeoutError' or bool(re.search(
        r'execution context was destroyed|frame was detached|frame has been detached', str(error), re.I))


async def authenticate(page, credentials, *, timeout=75, clock=time.monotonic, pause=asyncio.sleep):
    stage, flags, completed = 'login_form', {}, set()
    try:
        await page.goto(ORIGIN + '/login', wait_until='domcontentloaded', timeout=30000)
        deadline, last_submit = clock() + timeout, 0.0
        while clock() < deadline:
            try:
                body = await guard_page(page)
                if await verify_account(page):
                    return safe_result('passed', 'account', account_verified=True, **flags)
                email = await one_field(page, EMAIL)
                password = await one_field(page, PASSWORD)
                fields = await visible(page.locator(OTP)) if TOTP_PROMPT.search(body) else []
            except Stopped:
                raise
            except Exception as error:
                if transient_read(error):
                    await pause(.5)
                    continue
                raise
            if password is not None:
                step = 'password'
            elif fields:
                step = 'totp'
            else:
                await pause(.5)
                continue
            if step in completed:
                if clock() - last_submit > 8:
                    raise Stopped('repeated_step')
                await pause(.5)
                continue
            stage = step
            if step == 'password':
                if email is None:
                    raise Stopped('unsupported_form')
                await check_form(page, email)
                await check_form(page, password)
                await email.fill(credentials.username)
                await password.fill(credentials.password)
                await submit(page, SIGN_IN)
            else:
                if credentials.totp is None:
                    return safe_result('blocked', stage, reason='missing_credentials',
                                       missing=OPTIONAL, **flags)
                if len(fields) not in (1, 6):
                    raise Stopped('unsupported_form')
                remaining = credentials.totp.period - time.time() % credentials.totp.period
                if remaining < 6:
                    await pause(remaining + .2)
                code = credentials.totp.code(time.time())
                for i, field in enumerate(fields):
                    await check_form(page, field)
                    await field.fill(code if len(fields) == 1 else code[i])
                await pause(.3)
                if await verify_account(page):
                    flags['totp_submitted'] = True
                    return safe_result('passed', 'account', account_verified=True, **flags)
                if await fields[-1].is_visible():
                    await submit(page, VERIFY)
            flags[step + '_submitted'] = True
            completed.add(step)
            last_submit = clock()
            await pause(.5)
        return safe_result('blocked', stage, reason='timeout', **flags)
    except Stopped as exc:
        return safe_result('blocked', stage, reason=exc.args[0], **flags)
    except Exception:
        # Browser errors may contain fill arguments. Never emit exception text.
        return safe_result('failed', stage, reason='runtime_error', **flags)


async def protect_requests(context):
    async def guard(route):
        request = route.request
        if (request.is_navigation_request() or request.method not in {'GET', 'HEAD', 'OPTIONS'}) and not trusted(request.url):
            await route.abort()
        else:
            await route.continue_()
    await context.route('**/*', guard)


async def inspect_controls(page):
    """Only static form metadata. No input values, rows, HTML, cookies or tokens."""
    await wait_verified_account(page)
    return await page.evaluate('''() => {
      const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const safe = x => String(x || '').replace(/\\s+/g,' ').trim().slice(0,160);
      const clean = x => { x=safe(x); return /@|password|wachtwoord|token|secret|csrf/i.test(x)?'':x; };
      return Array.from(document.querySelectorAll('button,input,select,[role="combobox"]'))
        .filter(e=>visible(e)&&!['password','email','hidden'].includes(e.type||'')&&e.autocomplete!=='one-time-code')
        .slice(0,120).map(e=>({
          tag:e.tagName.toLowerCase(), type:clean(e.type), role:clean(e.getAttribute('role')),
          id:clean(e.id), name:clean(e.name),
          label:clean(e.getAttribute('aria-label')||Array.from(e.labels||[]).map(x=>x.textContent).join(' ')||
            (e.tagName==='BUTTON'?e.innerText:'')),
          options:e.tagName==='SELECT'?Array.from(e.options).slice(0,80).map(o=>({text:clean(o.text)})):undefined
        })); }''')


async def inspect_export_pages(page, result=None, progress=None):
    """Navigate only observed account links; do not submit any export yet."""
    result = {} if result is None else result
    for label in ('Payments', 'Refunds', 'Statements'):
        if progress is not None:
            progress['stage'] = label.lower()
        await wait_verified_account(page)
        link = await unique_account_link(page, label)
        if link is None:
            raise Stopped('unsupported_form')
        href = await link.get_attribute('href')
        url = urljoin(page.url, href or '')
        if not href or not account_page(url):
            raise Stopped('unexpected_origin')
        await link.click()
        # The portal can update through AJAX; wait for the observed target path.
        await page.wait_for_url(lambda current: urlsplit(str(current)).path == urlsplit(url).path, timeout=15000)
        await page.wait_for_load_state('domcontentloaded')
        result[label.lower()] = {'path': urlsplit(page.url).path,
                                 'controls': await inspect_controls(page)}
    return result


async def worker():
    stage = 'configuration'
    forms, flags, progress, diagnostics = {}, {}, {}, {}
    try:
        credentials = Credentials.from_env(os.environ)
        from playwright.async_api import async_playwright
        stage = 'browser_launch'
        child_env = {k:v for k,v in os.environ.items() if k in
                     {'PATH','HOME','TMPDIR','LANG','LD_LIBRARY_PATH','PLAYWRIGHT_BROWSERS_PATH'}}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, env=child_env)
            try:
                context = await browser.new_context(accept_downloads=False, service_workers='block')
                await protect_requests(context)
                page = await context.new_page()
                page.set_default_timeout(10000)
                login = await authenticate(page, credentials)
                if not login['account_verified']:
                    return {'result': login, 'forms': {}}
                flags = {k:login[k] for k in ('password_submitted','totp_submitted','account_verified')}
                stage = 'payments'
                try:
                    await inspect_export_pages(page, forms, progress)
                except Exception:
                    diagnostics = await account_diagnostics(page)
                    raise
                return {'result': safe_result('passed', 'complete',
                         **flags,
                         pages=forms), 'forms': forms}
            finally:
                await browser.close()
    except Stopped as exc:
        return {'result':safe_result('blocked',progress.get('stage',stage),reason=exc.args[0],
                **flags, diagnostics=diagnostics, pages=forms,
                missing=[n for n in REQUIRED if not os.environ.get(n)]),'forms':forms}
    except Exception:
        return {'result':safe_result('failed',progress.get('stage',stage),reason='runtime_error',
                **flags, diagnostics=diagnostics, pages=forms),'forms':forms}
