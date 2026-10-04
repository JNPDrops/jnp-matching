"""Render-only Exact username/password/TOTP login; no accounting actions.

Credentials, cookies, OTPs, page text and exception messages never leave this
module. The caller receives a small, enumerated result. A fresh browser context
is used for each job; an authenticated Page may be reused only inside that job.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import re
import time
from urllib.parse import parse_qs, urlsplit

from operations.paragon_login_probe import parse_totp

DIVISION = "3977752"
TARGET = "https://start.exactonline.nl/docs/MenuPortal.aspx?_Division_=" + DIVISION
HOSTS = {"start.exactonline.nl", "login.exact.com"}
ENV_NAMES = ("EXACT_WEB_USERNAME", "EXACT_WEB_PASSWORD", "EXACT_WEB_TOTP_SECRET")
USERNAME = 'input[name="LoginForm$UserName"], input[autocomplete="username"], input[name="signInName"]'
PASSWORD = 'input[type="password"]'
OTP = 'input[autocomplete="one-time-code"], input[name="otpCode"], input[id="otpCode"], input[name="VerificationCode"], input[id="verificationCode"]'
SUBMIT = re.compile(r"^(Continue|Sign in|Log in|Login|Next|Verify|Doorgaan|Inloggen|Aanmelden|Volgende|Verifiëren)$", re.I)
CAPTCHA = 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"], .g-recaptcha, .h-captcha'
BLOCK_TEXT = re.compile(r"verify you are human|checking your browser|unusual traffic|verifieer dat je een mens|controleer of je een mens", re.I)
ERROR_TEXT = re.compile(r"incorrect password|invalid password|invalid verification code|invalid code|incorrect code|ongeldig.{0,20}(wachtwoord|code)|onjuist.{0,20}(wachtwoord|code)|something went wrong|an error occurred|er is een fout opgetreden", re.I)
AUTH_TEXT = re.compile(r"sign in|log in|inloggen|aanmelden|verification code|verificatiecode|verificatie.code|two.step|tweestaps|authenticator", re.I)
LOGGED_OUT = re.compile(r"you.ve been logged out|please log in to continue|u bent uitgelogd|je bent uitgelogd", re.I)
OTP_TEXT = re.compile(r"authenticator|verification code|verificatie.code|verification.code|security code|beveiligingscode", re.I)
TOTP_APP_TEXT = re.compile(r"authenticator|authentication app|authenticatie.?app|verificatie.?app", re.I)


class LoginStopped(Exception):
    """Only an enumerated reason is allowed, never raw page/exception text."""
    def __init__(self, reason):
        super().__init__(reason if reason in REASONS else "runtime_error")


REASONS = {"none", "missing_credentials", "invalid_configuration", "unexpected_origin",
           "verification_required", "site_error", "unsupported_form", "repeated_step",
           "timeout", "wrong_administration", "runtime_error"}
STAGES = {"configuration", "browser_install", "browser_launch", "login_form", "username",
          "password", "totp", "administration", "complete", "claim", "runtime"}
STATUSES = {"disabled", "ready", "started", "passed", "blocked", "failed", "skipped"}
SIGNAL_PATTERNS = {
    'code_rejected': re.compile(r'(code|token).{0,70}(incorrect|invalid|expired|not valid|not correct|does not match|did not match|ongeldig|onjuist|verlopen)|(incorrect|invalid|expired|wrong|ongeldig|onjuist|verlopen).{0,40}(code|token)', re.I),
    'credentials_rejected': re.compile(r'(password|wachtwoord).{0,40}(incorrect|invalid|wrong|ongeldig|onjuist)|(incorrect|invalid|wrong|ongeldig|onjuist).{0,40}(password|wachtwoord)', re.I),
    'account_locked': re.compile(r'account.{0,35}(locked|blocked|geblokkeerd)|too many attempts|te veel pogingen', re.I),
    'authenticator_prompt': TOTP_APP_TEXT,
    'email_code_prompt': re.compile(r'(send|sent|email|e-mail|verzonden|gestuurd).{0,60}(code|email|e-mail)', re.I),
    'code_prompt': OTP_TEXT,
    'method_selection': re.compile(r'choose.{0,30}(method|option)|select.{0,30}(method|option)|kies.{0,30}(methode|optie)', re.I),
    'terms_prompt': re.compile(r'accept.{0,30}(terms|conditions)|accepteer.{0,30}voorwaarden', re.I),
    'stay_signed_in_prompt': re.compile(r'stay signed in|keep me signed in|aangemeld blijven|ingelogd blijven', re.I),
}
SIGNALS = set(SIGNAL_PATTERNS) | {'password_field', 'standard_otp_field', 'username_field',
    'administration_header', 'expected_portal_url', 'exact_login_origin'}


def safe_result(status, stage, *, reason="none", username_submitted=False,
                password_submitted=False, totp_submitted=False,
                administration_verified=False, missing=(), signals=()):
    if status not in STATUSES or stage not in STAGES or reason not in REASONS:
        raise ValueError("invalid_result")
    return {"status": status, "stage": stage, "reason": reason,
            "financial_writes": False, "fresh_session": True,
            "username_submitted": username_submitted is True,
            "password_submitted": password_submitted is True,
            "totp_submitted": totp_submitted is True,
            "administration_verified": administration_verified is True,
            "missing": [name for name in missing if name in ENV_NAMES],
            "signals": sorted({signal for signal in signals if isinstance(signal, str) and signal in SIGNALS})}


async def collect_signals(page):
    """Classify the last visible page using fixed labels; never emit its text."""
    signals = set()
    try:
        if target_page(page.url):
            signals.add('expected_portal_url')
        if urlsplit(page.url).hostname == 'login.exact.com':
            signals.add('exact_login_origin')
        for frame in page.frames[:6]:
            if not trusted(frame.url):
                continue
            body = await frame.locator('body').inner_text(timeout=1000)
            signals.update(key for key, pattern in SIGNAL_PATTERNS.items() if pattern.search(body))
            for selector, key in [(PASSWORD, 'password_field'), (OTP, 'standard_otp_field'),
                                  (USERNAME, 'username_field'), ('#Administration', 'administration_header')]:
                if await visible(frame.locator(selector)):
                    signals.add(key)
    except Exception:
        pass
    return sorted(signals)


def trusted(url):
    try:
        p = urlsplit(url)
        return p.scheme == "https" and p.hostname in HOSTS and p.port in (None, 443) and not p.username and not p.password
    except (ValueError, TypeError):
        return False


def target_page(url):
    if not trusted(url):
        return False
    p = urlsplit(url)
    return (p.hostname == "start.exactonline.nl" and p.path.lower() == "/docs/menuportal.aspx"
            and parse_qs(p.query).get("_Division_") == [DIVISION])


def form_action_allowed(action):
    # AJAX sign-in forms can deliberately have a no-op form action. This is
    # not a network destination; the request guard still restricts all auth
    # navigation/POST traffic to the existing Exact allowlist.
    return trusted(action) or bool(isinstance(action, str) and re.fullmatch(
        r'javascript:\s*void\s*\(\s*0\s*\)\s*;?', action.strip(), re.I))


@dataclass(repr=False)
class Credentials:
    username: str
    password: str
    totp: object

    @classmethod
    def from_env(cls, environ):
        if any(not environ.get(name) for name in ENV_NAMES):
            raise LoginStopped("missing_credentials")
        try:
            generator = parse_totp(environ["EXACT_WEB_TOTP_SECRET"])
            if generator.digits != 6:
                raise ValueError()
            username = environ["EXACT_WEB_USERNAME"].strip()
            if not username or len(username) > 256:
                raise ValueError()
            return cls(username, environ["EXACT_WEB_PASSWORD"], generator)
        except Exception:
            raise LoginStopped("invalid_configuration") from None


async def visible(locator):
    return [item for item in await locator.all() if await item.is_visible()]


async def one_input(frame, selector):
    fields = [field for field in await visible(frame.locator(selector)) if await field.is_enabled()]
    if len(fields) > 1:
        raise LoginStopped("unsupported_form")
    return fields[0] if fields else None


async def locate_otp(frame, *, after_password):
    fields = [field for field in await visible(frame.locator(OTP)) if await field.is_enabled()]
    if fields or not after_password:
        return fields
    # Exact can render an authenticator field without a standard OTP name or
    # autocomplete attribute. Use the visible form meaning, only after password
    # submission and only when it explicitly names an authenticator app.
    text = await frame.locator('body').inner_text()
    if not TOTP_APP_TEXT.search(text):
        return []
    candidates = [field for field in await visible(frame.locator(
        'input[type="text"], input[type="tel"], input[type="number"], input:not([type])'))
        if await field.is_enabled() and await field.get_attribute('autocomplete') != 'username']
    if len(candidates) == 1:
        return candidates
    if len(candidates) == 6 and all([await field.get_attribute('maxlength') == '1' for field in candidates]):
        return candidates
    if candidates:
        raise LoginStopped('unsupported_form')
    return []


async def guard_page(page):
    if not trusted(page.url):
        raise LoginStopped("unexpected_origin")
    # Inspect for blocks before filtering frames to the allowed credential hosts.
    for frame in page.frames:
        if await visible(frame.locator(CAPTCHA)):
            raise LoginStopped("verification_required")
        if trusted(frame.url):
            text = await frame.locator("body").inner_text(timeout=2000)
            if BLOCK_TEXT.search(text):
                raise LoginStopped("verification_required")
            if ERROR_TEXT.search(text):
                raise LoginStopped("site_error")


async def verify_administration(page):
    """A cached menu wrapped around an expired iframe is NOT a valid login."""
    if not target_page(page.url):
        return False
    company = page.locator('#Administration')
    if await company.count() != 1 or not await company.is_visible():
        return False
    if "James n Parson B.V." not in await company.inner_text():
        raise LoginStopped("wrong_administration")
    body = await page.locator('body').inner_text()
    if LOGGED_OUT.search(body):
        return False
    frames = [frame for frame in page.frames if frame.name == "MainWindow"]
    if len(frames) != 1:
        return False
    main = frames[0]
    p = urlsplit(main.url)
    if not trusted(main.url) or p.hostname != "start.exactonline.nl" or not p.path.lower().startswith('/docs/'):
        return False
    if not (await main.locator('body').inner_text()).strip():
        return False
    for frame in page.frames:
        if trusted(frame.url):
            if await visible(frame.locator(USERNAME + ', ' + PASSWORD + ', ' + OTP)):
                return False
            text = await frame.locator('body').inner_text()
            if LOGGED_OUT.search(text) or AUTH_TEXT.search(await frame.title()):
                return False
    return await page.locator('#EnhancedNavigation').is_visible()


async def check_destination(page, frame, field):
    if not trusted(page.url) or not trusted(frame.url):
        raise LoginStopped("unexpected_origin")
    # Check the form action immediately before filling. Never send credentials
    # to an off-origin form, even if it is rendered in a trusted document.
    action = await field.evaluate("el => el.form ? el.form.action : location.href")
    if not form_action_allowed(action):
        raise LoginStopped("unexpected_origin")


async def submit(page, frame, field):
    await check_destination(page, frame, field)
    buttons = await visible(frame.get_by_role('button', name=SUBMIT))
    if len(buttons) != 1 or not await buttons[0].is_enabled():
        raise LoginStopped("unsupported_form")
    # Overrides such as formaction must be checked as well as the parent form.
    action = await buttons[0].evaluate("el => el.hasAttribute('formaction') ? el.formAction : (el.form ? el.form.action : location.href)")
    if not form_action_allowed(action):
        raise LoginStopped("unexpected_origin")
    await buttons[0].click()


async def authenticate(page, credentials, *, timeout=75, clock=time.monotonic, pause=asyncio.sleep):
    stage = "login_form"
    completed = set()
    flags = {}
    try:
        await page.goto(TARGET, wait_until='domcontentloaded', timeout=30000)
        deadline = clock() + timeout
        last_submit = 0.0
        while clock() < deadline:
            await guard_page(page)
            if await verify_administration(page):
                return safe_result('passed', 'complete', administration_verified=True, **flags)
            progressed = False
            for frame in page.frames:
                if not trusted(frame.url):
                    continue
                password = await one_input(frame, PASSWORD)
                username = await one_input(frame, USERNAME)
                otp_fields = await locate_otp(frame, after_password='password' in completed and password is None)
                if password is not None:
                    step = 'password'
                elif otp_fields:
                    step = 'totp'
                elif username is not None:
                    step = 'username'
                else:
                    continue
                if step in completed:
                    # A DOM may remain visible briefly after clicking submit.
                    if clock() - last_submit > 8:
                        raise LoginStopped('repeated_step')
                    continue
                stage = step
                if step == 'password':
                    if username is not None:
                        await check_destination(page, frame, username)
                        await username.fill(credentials.username)
                    await check_destination(page, frame, password)
                    await password.fill(credentials.password)
                    await submit(page, frame, password)
                elif step == 'username':
                    await check_destination(page, frame, username)
                    await username.fill(credentials.username)
                    await submit(page, frame, username)
                else:
                    text = await frame.locator('body').inner_text()
                    if not OTP_TEXT.search(text) or len(otp_fields) not in (1, 6):
                        raise LoginStopped('unsupported_form')
                    remaining = credentials.totp.period - time.time() % credentials.totp.period
                    if remaining < 6:
                        await pause(remaining + 0.2)
                    code = credentials.totp.code(time.time())
                    for i, field in enumerate(otp_fields):
                        await check_destination(page, frame, field)
                        await field.fill(code if len(otp_fields) == 1 else code[i])
                    # A code widget can submit itself. Do not click a new page.
                    await pause(0.3)
                    if await verify_administration(page):
                        flags['totp_submitted'] = True
                        return safe_result('passed', 'complete', administration_verified=True, **flags)
                    if await otp_fields[-1].is_visible():
                        await submit(page, frame, otp_fields[-1])
                flags[step + '_submitted'] = True
                completed.add(step)
                last_submit = clock()
                progressed = True
                break
            await pause(0.25 if progressed else 0.5)
        return safe_result('blocked', stage, reason='timeout', signals=await collect_signals(page), **flags)
    except LoginStopped as exc:
        return safe_result('blocked', stage, reason=exc.args[0], signals=await collect_signals(page), **flags)
    except Exception:
        # Playwright errors may contain fill values, URLs, or page contents.
        return safe_result('failed', stage, reason='runtime_error', signals=await collect_signals(page), **flags)


async def protect_requests(context):
    async def guard(route):
        request = route.request
        credential_request = request.is_navigation_request() or request.method not in {'GET', 'HEAD', 'OPTIONS'}
        if credential_request and not trusted(request.url):
            await route.abort()
        else:
            await route.continue_()
    await context.route('**/*', guard)


async def probe_worker():
    """Only the isolated Render child process calls this. Never import a file."""
    stage = 'configuration'
    try:
        credentials = Credentials.from_env(os.environ)
        from playwright.async_api import async_playwright
        stage = 'browser_launch'
        async with async_playwright() as playwright:
            # Browser children do not inherit accounting or login secrets.
            child_env = {k: v for k, v in os.environ.items() if k in
                         {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LD_LIBRARY_PATH', 'PLAYWRIGHT_BROWSERS_PATH'}}
            browser = await playwright.chromium.launch(headless=True, env=child_env)
            try:
                context = await browser.new_context(accept_downloads=False, service_workers='block')
                await protect_requests(context)
                page = await context.new_page()
                page.set_default_timeout(10000)
                return await authenticate(page, credentials)
            finally:
                await browser.close()
    except LoginStopped as exc:
        return safe_result('blocked', stage, reason=exc.args[0], missing=[n for n in ENV_NAMES if not os.environ.get(n)])
    except Exception:
        return safe_result('failed', stage, reason='runtime_error')
