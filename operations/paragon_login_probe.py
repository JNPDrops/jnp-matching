"""One explicitly requested, expiring login probe; never imports transactions.

Secrets are consumed only inside Render. No cookies, page contents, screenshots,
exception text, passwords, or OTP values are returned or logged. The database
claim is committed before attempting authentication, preventing login retries
on restarts or concurrent deployments.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import signal
import struct
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

PROBE_ID = "paragon-login-20261004-v1"
EXPIRES_AT = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
ORIGIN = "https://paragon.online"
ENV_NAMES = ("PARAGON_EMAIL", "PARAGON_USERNAME", "PARAGON_PASSWORD", "PARAGON_TOTP_SECRET")
LOG = logging.getLogger("uvicorn.error")


@dataclass(repr=False)
class Totp:
    key: bytes
    algorithm: str = "sha1"
    digits: int = 6
    period: int = 30

    def code(self, timestamp: float) -> str:
        counter = int(timestamp) // self.period
        digest = hmac.new(self.key, struct.pack(">Q", counter), getattr(hashlib, self.algorithm)).digest()
        offset = digest[-1] & 15
        number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
        return str(number % (10 ** self.digits)).zfill(self.digits)


def parse_totp(value: str) -> Totp:
    algorithm, digits, period = "sha1", 6, 30
    secret = value.strip()
    if secret.lower().startswith("otpauth://"):
        uri = urlsplit(secret)
        if uri.netloc != "totp":
            raise ValueError("invalid_totp_configuration")
        params = parse_qs(uri.query, strict_parsing=True)
        if any(len(v) != 1 for v in params.values()):
            raise ValueError("invalid_totp_configuration")
        secret = params.get("secret", [""])[0]
        algorithm = params.get("algorithm", ["SHA1"])[0].lower()
        digits = int(params.get("digits", ["6"])[0])
        period = int(params.get("period", ["30"])[0])
    secret = re.sub(r"\s+", "", secret).upper().rstrip("=")
    if not re.fullmatch(r"[A-Z2-7]{16,256}", secret):
        raise ValueError("invalid_totp_configuration")
    if algorithm not in {"sha1", "sha256", "sha512"} or digits not in {6, 8} or not 15 <= period <= 120:
        raise ValueError("invalid_totp_configuration")
    try:
        key = base64.b32decode(secret + "=" * ((-len(secret)) % 8))
    except Exception:
        raise ValueError("invalid_totp_configuration") from None
    return Totp(key, algorithm, digits, period)


def eligible(environ, now: datetime) -> bool:
    return environ.get("PARAGON_LOGIN_PROBE_ID") == PROBE_ID and now < EXPIRES_AT


def missing_credentials(environ) -> list[str]:
    return [name for name in ENV_NAMES if not environ.get(name)]


def is_page(url: str, path: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme, parsed.netloc, parsed.path) == ("https", "paragon.online", path)


def safe_result(status: str, stage: str, **kwargs) -> dict:
    allowed_statuses = {"started", "passed", "blocked", "failed", "skipped"}
    allowed_stages = {"claim", "configuration", "browser_install", "browser_launch", "login_form",
                      "password", "totp_form", "totp", "dashboard", "complete", "runtime"}
    if status not in allowed_statuses or stage not in allowed_stages:
        raise ValueError("invalid_probe_result")
    result = {"probe": PROBE_ID, "status": status, "stage": stage,
              "fresh_session": True, "financial_writes": False}
    for key in ("password_accepted", "totp_submitted", "dashboard_verified"):
        if key in kwargs:
            result[key] = kwargs[key] is True
    if "missing" in kwargs:
        result["missing"] = [name for name in kwargs["missing"] if name in ENV_NAMES]
    return result


def emit(result: dict) -> None:
    LOG.warning("PARAGON_LOGIN_PROBE %s", json.dumps(result, sort_keys=True))


def claim_probe(database_url: str) -> bool:
    import psycopg
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS paragon_login_probes (
            probe_id TEXT PRIMARY KEY,
            attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            result JSONB NOT NULL
        )""")
        row = conn.execute("""INSERT INTO paragon_login_probes (probe_id, result)
            VALUES (%s, %s::jsonb) ON CONFLICT DO NOTHING RETURNING probe_id""",
            (PROBE_ID, json.dumps(safe_result("started", "claim")))).fetchone()
    return row is not None


def store_result(database_url: str, result: dict) -> None:
    import psycopg
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute("UPDATE paragon_login_probes SET result=%s::jsonb WHERE probe_id=%s",
                     (json.dumps(result), PROBE_ID))


async def worker() -> dict:
    stage = "configuration"
    password_accepted = totp_submitted = dashboard_verified = False
    browser = None
    installer = None
    try:
        missing = missing_credentials(os.environ)
        if missing:
            return safe_result("blocked", stage, missing=missing)
        totp = parse_totp(os.environ["PARAGON_TOTP_SECRET"])
        # The real Paragon form, observed on 2026-10-04, has six fields.
        if totp.digits != 6:
            return safe_result("blocked", stage)

        stage = "browser_install"
        browser_dir = Path(tempfile.gettempdir()) / "jnp-paragon-browser-1.63.0"
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
        # The installer does not need either Paragon or Exact credentials.
        install_env = {k: v for k, v in os.environ.items()
                       if k in {"PATH", "HOME", "TMPDIR", "LANG", "LD_LIBRARY_PATH", "PLAYWRIGHT_BROWSERS_PATH"}}
        installer = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium", "--only-shell",
            env=install_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            code = await asyncio.wait_for(installer.wait(), timeout=150)
        except asyncio.TimeoutError:
            installer.kill()
            await installer.wait()
            return safe_result("blocked", stage)
        if code:
            return safe_result("blocked", stage)

        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            stage = "browser_launch"
            browser = await playwright.chromium.launch(headless=True, env=install_env)
            try:
                # No persisted context, storage state, cookies, traces, or video.
                context = await browser.new_context(accept_downloads=False)
                page = await context.new_page()
                page.set_default_timeout(15000)
                stage = "login_form"
                await page.goto(ORIGIN + "/login", wait_until="domcontentloaded", timeout=30000)
                await page.locator('input[name="email"]').wait_for(state="visible")
                if not is_page(page.url, "/login"):
                    return safe_result("blocked", stage)
                await page.locator('input[name="email"]').fill(os.environ["PARAGON_EMAIL"])
                await page.locator('input[name="username"]').fill(os.environ["PARAGON_USERNAME"])
                await page.locator('input[name="password"]').fill(os.environ["PARAGON_PASSWORD"])
                stage = "password"
                await page.get_by_role("button", name="Login", exact=True).click()
                await page.wait_for_url(lambda url: is_page(str(url), "/2fa"), timeout=30000)
                password_accepted = True
                stage = "totp_form"
                inputs = page.locator('input[autocomplete="one-time-code"]')
                await inputs.first.wait_for(state="visible")
                if not is_page(page.url, "/2fa") or await inputs.count() != 6:
                    return safe_result("blocked", stage, password_accepted=True)
                # Avoid submitting a code at the end of its validity window.
                remaining = totp.period - time.time() % totp.period
                if remaining < 6:
                    await asyncio.sleep(remaining + 0.2)
                code = totp.code(time.time())
                for index, digit in enumerate(code):
                    await inputs.nth(index).fill(digit)
                stage = "totp"
                await page.get_by_role("button", name="Login", exact=True).click()
                totp_submitted = True
                await page.wait_for_url(lambda url: is_page(str(url), "/dashboard"), timeout=30000)
                stage = "dashboard"
                await page.get_by_role("heading", name="Dashboard", exact=True).wait_for(state="visible")
                await page.get_by_role("button", name=re.compile(r"^Hello .+!$")).wait_for(state="visible")
                dashboard_verified = True
            finally:
                await browser.close()
                browser = None
        return safe_result("passed", "complete", password_accepted=True,
                           totp_submitted=True, dashboard_verified=True)
    except Exception:
        # Playwright errors can quote credential arguments. Never log them.
        return safe_result("failed", stage, password_accepted=password_accepted,
                           totp_submitted=totp_submitted, dashboard_verified=dashboard_verified)


async def run() -> None:
    """Optional background task; a durable claim limits this probe to one run."""
    if not eligible(os.environ, datetime.now(timezone.utc)):
        return
    missing = missing_credentials(os.environ)
    if missing:
        emit(safe_result("blocked", "configuration", missing=missing))
        return
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        emit(safe_result("blocked", "claim"))
        return
    try:
        claimed = await asyncio.to_thread(claim_probe, database_url)
    except Exception:
        emit(safe_result("blocked", "claim"))
        return
    if not claimed:
        emit(safe_result("skipped", "claim"))
        return
    emit(safe_result("started", "runtime"))
    child = None
    result = safe_result("failed", "runtime")
    try:
        child_env = {k: v for k, v in os.environ.items()
                     if k in {"PATH", "HOME", "TMPDIR", "LANG", "LD_LIBRARY_PATH", "VIRTUAL_ENV", *ENV_NAMES}}
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "operations.paragon_login_probe", "--worker",
            env=child_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        stdout, _ = await asyncio.wait_for(child.communicate(), timeout=300)
        if child.returncode == 0 and len(stdout) < 4096:
            data = json.loads(stdout)
            result = safe_result(data.pop("status"), data.pop("stage"), **data)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if child.returncode is None:
                await child.wait()
        emit(result)
        try:
            await asyncio.to_thread(store_result, database_url, result)
        except Exception:
            emit(safe_result("failed", "claim"))


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    print(json.dumps(asyncio.run(worker()), sort_keys=True))
