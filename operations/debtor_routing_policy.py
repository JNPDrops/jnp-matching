"""Operator routes and the 21:34 authorization to process historic ICEPAY now."""
from datetime import datetime, timezone

REVISION = '2026-10-03-allocation-icepay-now-v2'
START_AT = datetime(2026, 10, 4, 0, 0, 5, tzinfo=timezone.utc)
CONTINUOUS_ROUTES = {'bacs': '109372', 'plisio': '109377', 'icepay-ideal': '109419'}
CLEANUP_ROUTES = {**CONTINUOUS_ROUTES, 'np_payments': '109421',
                  'suap_wordpresspayplugin': '109422'}
RETAIN_ON_SOURCE = {'wc_fibonatix': '100100', 'wc_fibonatics': '100100'}


def routes(scope):
    if scope == 'continuous': return CONTINUOUS_ROUTES
    if scope == 'cleanup': return CLEANUP_ROUTES
    return {}


def icepay_only(now=None):
    return (now or datetime.now(timezone.utc)) < START_AT


def routes_for_now(now=None):
    return {'icepay-ideal':'109419'} if icepay_only(now) else dict(CLEANUP_ROUTES)


def work_allowed(method, scope, now=None):
    return method in routes(scope) and (not icepay_only(now)
        or (method=='icepay-ideal' and scope=='cleanup'))


def initialize(conn):
    conn.execute('ALTER TABLE jnp_debtor_route_control ADD COLUMN IF NOT EXISTS routing_policy TEXT')
    conn.execute('ALTER TABLE jnp_debtor_route_control ADD COLUMN IF NOT EXISTS policy_activated_at TIMESTAMPTZ')
    conn.execute('ALTER TABLE jnp_debtor_route_control ADD COLUMN IF NOT EXISTS open_item_cleanup JSONB')
    conn.execute('ALTER TABLE jnp_debtor_route_queue ADD COLUMN IF NOT EXISTS order_evidence JSONB')
    conn.execute("ALTER TABLE jnp_debtor_route_queue ADD COLUMN IF NOT EXISTS work_scope TEXT NOT NULL DEFAULT 'continuous'")
    conn.execute('ALTER TABLE jnp_debtor_route_queue ADD COLUMN IF NOT EXISTS order_reference TEXT')
    conn.execute('ALTER TABLE jnp_debtor_route_queue ADD COLUMN IF NOT EXISTS entry_type INTEGER NOT NULL DEFAULT 20')
    conn.execute('ALTER TABLE jnp_debtor_route_queue ADD COLUMN IF NOT EXISTS routing_policy TEXT')
    conn.execute('ALTER TABLE jnp_debtor_route_queue ADD COLUMN IF NOT EXISTS debit_entry_id UUID')


def activate_once(conn):
    """Explicit new authorization, not an override of later manual pauses."""
    from operations import bacs_debtor_transfer as m, routing_runtime as runtime
    with conn.transaction():
        row = conn.execute('SELECT started_at,cursor_at,routing_policy,pause_reason FROM jnp_debtor_route_control FOR UPDATE').fetchone()
        m.require(row is not None and row[0] is not None and row[1] is not None,
                  'Previously initialized routing worker required')
        if row[2] == REVISION: return
        conn.execute('UPDATE jnp_debtor_route_control SET enabled=TRUE,pause_reason=NULL,routing_policy=%s,policy_activated_at=NOW(),open_item_cleanup=NULL', (REVISION,))
        conn.execute("UPDATE jnp_debtor_route_queue SET state='pending',reason=NULL,next_check=NOW() WHERE state='paused'")
    runtime.event('authorized_policy_activation', revision=REVISION,
                  previous_pause=runtime.pause_code(row[3]), start_at=START_AT,
                  continuous_routes=CONTINUOUS_ROUTES, cleanup_routes=CLEANUP_ROUTES)


def wait_for_start(status, now=None):
    now = now or datetime.now(timezone.utc)
    if now < START_AT:
        status.update(state='scheduled', next_attempt_at=START_AT.isoformat())
        return True
    return False
