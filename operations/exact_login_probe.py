"""One explicitly enabled, expiring Exact login attempt on existing Render.

No public execution endpoint. Claim before login, no retries on restarts, no
shared cookies or persisted browser profile. No import or matching writes.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
import tempfile

from operations.exact_browser import ENV_NAMES, Credentials, probe_worker, safe_result

PROBE_ID = 'exact-login-20261004-v8'
EXPIRES_AT = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
LOG = logging.getLogger('uvicorn.error')
STATUS = safe_result('disabled', 'configuration')


def eligible(environ, now):
    return environ.get('EXACT_LOGIN_PROBE_ID') == PROBE_ID and now < EXPIRES_AT


def publish(result):
    STATUS.clear()
    STATUS.update(result)
    LOG.warning('EXACT_LOGIN_PROBE %s', json.dumps(result, sort_keys=True))


def claim_probe(database_url):
    import psycopg
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS exact_login_probes (
            probe_id TEXT PRIMARY KEY, attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            result JSONB NOT NULL)''')
        row = conn.execute('''INSERT INTO exact_login_probes(probe_id,result)
            VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING probe_id''',
            (PROBE_ID, json.dumps(safe_result('started', 'claim')))).fetchone()
    return row is not None


def store_result(database_url, result):
    import psycopg
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute('UPDATE exact_login_probes SET result=%s::jsonb WHERE probe_id=%s',
                     (json.dumps(result), PROBE_ID))


async def stop_child(child):
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if child.returncode is None:
        await child.wait()


async def run():
    if not eligible(os.environ, datetime.now(timezone.utc)):
        return
    missing = [n for n in ENV_NAMES if not os.environ.get(n)]
    if missing:
        publish(safe_result('blocked', 'configuration', reason='missing_credentials', missing=missing))
        return
    try:
        Credentials.from_env(os.environ)
    except Exception:
        publish(safe_result('blocked', 'configuration', reason='invalid_configuration'))
        return
    database_url = os.environ.get('DATABASE_URL', '')
    if not database_url:
        publish(safe_result('blocked', 'claim', reason='invalid_configuration'))
        return
    try:
        claimed = await asyncio.to_thread(claim_probe, database_url)
    except Exception:
        publish(safe_result('blocked', 'claim', reason='runtime_error'))
        return
    if not claimed:
        publish(safe_result('skipped', 'claim'))
        return
    publish(safe_result('started', 'browser_install'))
    child = None
    result = safe_result('failed', 'browser_install', reason='runtime_error')
    # Reuse the existing pinned Paragon browser installation directory.
    browser_dir = str(Path(tempfile.gettempdir()) / 'jnp-paragon-browser-1.63.0')
    base_env = {k: v for k, v in os.environ.items() if k in
                {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LD_LIBRARY_PATH', 'VIRTUAL_ENV'}}
    base_env['PLAYWRIGHT_BROWSERS_PATH'] = browser_dir
    try:
        child = await asyncio.create_subprocess_exec(sys.executable, '-m', 'playwright',
            'install', 'chromium', '--only-shell', env=base_env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        if await asyncio.wait_for(child.wait(), timeout=150) != 0:
            return
        child = None
        publish(safe_result('started', 'runtime'))
        result = safe_result('failed', 'runtime', reason='runtime_error')
        child_env = {**base_env, **{name: os.environ[name] for name in ENV_NAMES}}
        child = await asyncio.create_subprocess_exec(sys.executable, '-m',
            'operations.exact_login_probe', '--worker', env=child_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        stdout, _ = await asyncio.wait_for(child.communicate(), timeout=150)
        if child.returncode == 0 and len(stdout) < 4096:
            raw = json.loads(stdout)
            # Reject extra fields, even if a future child implementation emits them.
            allowed = {'status', 'stage', 'reason', 'username_submitted', 'password_submitted',
                       'totp_submitted', 'administration_verified', 'missing', 'signals'}
            result = safe_result(**{k: v for k, v in raw.items() if k in allowed})
    except asyncio.CancelledError:
        result = safe_result('failed', 'runtime', reason='runtime_error')
        raise
    except Exception:
        pass
    finally:
        await stop_child(child)
        publish(result)
        try:
            await asyncio.to_thread(store_result, database_url, result)
        except Exception:
            publish(safe_result('failed', 'claim', reason='runtime_error'))


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    print(json.dumps(asyncio.run(probe_worker()), sort_keys=True))
