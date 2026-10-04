"""Expiring ICEPAY login/export-form probe on the existing Render service.

Secrets stay in an isolated child, results are enumerated, form metadata is
stored privately, and a durable claim prevents repeated login after restarts.
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

from operations.icepay_browser import (
    Credentials, ENV_NAMES, REQUIRED, FORM_NAMES, safe_result, validate_result, worker)

PROBE_ID = 'icepay-fetch-20261001-03-v5'
ACTIVATION = 'ICEPAY_FETCH_PROBE_ID'
EXPIRES = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)
PERIOD = {'from': '2026-10-01', 'through': '2026-10-03', 'timezone': 'Europe/Amsterdam'}
LOG = logging.getLogger('uvicorn.error')
STATUS = safe_result('disabled', 'configuration')
BASE_ENV = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LD_LIBRARY_PATH', 'VIRTUAL_ENV'}


def eligible(environ, now):
    return environ.get(ACTIVATION) == PROBE_ID and now < EXPIRES


def environments(environ):
    base = {k:v for k,v in environ.items() if k in BASE_ENV}
    base['PLAYWRIGHT_BROWSERS_PATH'] = str(Path(tempfile.gettempdir()) / 'jnp-paragon-browser-1.63.0')
    child = {**base, **{k:environ[k] for k in ENV_NAMES if environ.get(k)}}
    return base, child


def publish(result):
    result = validate_result(result)
    STATUS.clear()
    STATUS.update(result)
    LOG.warning('ICEPAY_FETCH_PROBE %s', json.dumps({'probe':PROBE_ID, **result}, sort_keys=True))


def validate_forms(forms):
    if not isinstance(forms, dict) or set(forms) - FORM_NAMES:
        raise ValueError('invalid_forms')
    for name, page in forms.items():
        if not isinstance(page, dict) or set(page) != {'path', 'controls'}:
            raise ValueError('invalid_page_metadata')
        if not page['path'].startswith('/merchant/88292/') or '?' in page['path']:
            raise ValueError('invalid_page_path')
        if not isinstance(page['controls'], list) or len(page['controls']) > 120:
            raise ValueError('invalid_controls')
        for c in page['controls']:
            if not isinstance(c, dict) or set(c) - {'tag','type','role','id','name','label','placeholder','options',
                                                   'context_label','readonly','date_preview'}:
                raise ValueError('invalid_control_fields')
            if c.get('date_preview') and c.get('id') not in {
                'tableFiltersForm.OrderTime.OrderTime','tableFiltersForm.PaymentTime.PaymentTime',
                'tableFiltersForm.DateCreated.DateCreated'}:
                raise ValueError('invalid_date_preview')
            for k, v in c.items():
                if k == 'options':
                    if not isinstance(v, list) or len(v) > 80 or any(
                        not isinstance(o, dict) or set(o) != {'text'} or not isinstance(o['text'], str)
                        or len(o['text']) > 160 for o in v):
                        raise ValueError('invalid_options')
                elif not isinstance(v, str) or len(v) > 160:
                    raise ValueError('invalid_metadata_text')
    return forms


def publish_forms(forms):
    # Render's DB has no external allowlist. Use the private log connector for
    # the same bounded static metadata, without exposing a public HTTP route.
    for name, page in validate_forms(forms).items():
        for start in range(0, len(page['controls']), 10):
            LOG.warning('ICEPAY_FORM_METADATA %s', json.dumps({
                'probe':PROBE_ID, 'page':name, 'path':page['path'], 'offset':start,
                'controls':page['controls'][start:start+10]}, sort_keys=True))


def decode_worker_output(stdout):
    try:
        if len(stdout) > 300000:
            raise ValueError()
        raw = json.loads(stdout)
        if not isinstance(raw, dict) or set(raw) != {'result', 'forms'}:
            raise ValueError()
        result = validate_result(raw['result'])
    except Exception:
        return safe_result('failed','runtime',reason='invalid_worker_output'), {}
    try:
        return result, validate_forms(raw['forms'])
    except Exception:
        # Preserve verified login evidence even if form serialization failed.
        result.update(status='failed', reason='invalid_form_metadata')
        return validate_result(result), {}


def claim(database_url):
    import psycopg
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS icepay_fetch_probes (
            probe_id TEXT PRIMARY KEY, attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            result JSONB NOT NULL, forms JSONB NOT NULL DEFAULT '{}'::jsonb)''')
        return conn.execute('''INSERT INTO icepay_fetch_probes(probe_id,result)
            VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING probe_id''',
            (PROBE_ID, json.dumps(safe_result('started','claim')))).fetchone() is not None


def save(database_url, result, forms):
    import psycopg
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        conn.execute('UPDATE icepay_fetch_probes SET result=%s::jsonb,forms=%s::jsonb WHERE probe_id=%s',
                     (json.dumps(validate_result(result)), json.dumps(validate_forms(forms)), PROBE_ID))


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
    missing = [n for n in REQUIRED if not os.environ.get(n)]
    if missing:
        publish(safe_result('blocked','configuration',reason='missing_credentials',missing=missing))
        return
    try:
        Credentials.from_env(os.environ)
    except Exception:
        publish(safe_result('blocked','configuration',reason='invalid_configuration'))
        return
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        publish(safe_result('blocked','claim',reason='invalid_configuration'))
        return
    try:
        claimed = await asyncio.to_thread(claim, database_url)
    except Exception:
        publish(safe_result('blocked','claim',reason='runtime_error'))
        return
    if not claimed:
        publish(safe_result('skipped','claim'))
        return
    child, forms = None, {}
    result = safe_result('failed','browser_install',reason='runtime_error')
    base, child_env = environments(os.environ)
    publish(safe_result('started','browser_install'))
    try:
        child = await asyncio.create_subprocess_exec(sys.executable, '-m', 'playwright',
            'install', 'chromium', '--only-shell', env=base,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        if await asyncio.wait_for(child.wait(), timeout=150) != 0:
            return
        child = None
        result = safe_result('failed','runtime',reason='runtime_error')
        publish(safe_result('started','runtime'))
        child = await asyncio.create_subprocess_exec(sys.executable, '-m',
            'operations.icepay_fetch_probe', '--worker', env=child_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        stdout, _ = await asyncio.wait_for(child.communicate(), timeout=180)
        if child.returncode == 0:
            result, forms = decode_worker_output(stdout)
        else:
            result = safe_result('failed','runtime',reason='worker_exit')
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        await stop_child(child)
        publish(result)
        publish_forms(forms)
        try:
            await asyncio.to_thread(save, database_url, result, forms)
        except Exception:
            publish(safe_result('failed','claim',reason='runtime_error'))


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    print(json.dumps(asyncio.run(worker()), sort_keys=True))
