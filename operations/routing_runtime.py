"""Operational recovery/status only; no Exact reads, writes or balance checks."""
from datetime import datetime, timedelta, timezone
import json
import logging

log = logging.getLogger('uvicorn.error')
RECOVERY_COLUMN = 'continuous_routing_recovered_at'


def pause_code(reason):
    return {
        'Write attempted without a complete verified audit; inspect durable audit': 'automatic_entry_unconfirmed',
        'Backfill write attempted without a complete verified audit; inspect durable archive': 'backfill_entry_unconfirmed',
        'Unresolved write intent: inspect durable audit before resuming': 'unresolved_entry_on_restart',
        'Paused by authorized operator': 'operator_pause',
        None: None,
    }.get(reason, 'other_stored_pause')


def event(name, **fields):
    # Callers pass only fixed reason codes and operational identifiers/counts.
    log.info('debtor_routing %s', json.dumps({'event': name, **fields}, default=str))


def queue_counts(conn):
    return {
        'new_entries': dict(conn.execute('SELECT state,COUNT(*) FROM jnp_debtor_route_queue GROUP BY state').fetchall()),
        'backfill': dict(conn.execute('SELECT state,COUNT(*) FROM jnp_debtor_route_backfill GROUP BY state').fetchall()),
    }


def recover_once(conn):
    """User-authorized 2026-10-03 resume, preserving cursor and every entry state.

    Only a previously initialized worker is resumed. The durable marker means a
    subsequent manual pause stays effective across cycles and deployments.
    Uncertain entries are NOT reset to pending and are never retried here.
    """
    with conn.transaction():
        row = conn.execute('SELECT enabled,started_at,cursor_at,pause_reason,continuous_routing_recovered_at FROM jnp_debtor_route_control FOR UPDATE').fetchone()
        enabled, started, cursor, reason, recovered = row
        if recovered is not None or started is None or cursor is None:
            return
        counts = queue_counts(conn)
        conn.execute('UPDATE jnp_debtor_route_control SET enabled=TRUE,pause_reason=NULL,continuous_routing_recovered_at=NOW()')
    event('authorized_continuous_resume', previously_enabled=enabled,
          previous_pause=pause_code(reason), preserved_cursor=cursor, queues=counts)


def defer_until_reset(status, limits, default_seconds=300):
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=default_seconds)
    remaining, reset = limits.get('remaining'), limits.get('reset_ms')
    if type(reset) is int and type(remaining) is int and remaining < 103:
        candidate = datetime.fromtimestamp(reset / 1000, timezone.utc)
        if now < candidate <= now + timedelta(hours=25):
            until = max(until, candidate + timedelta(seconds=5))
    status.update(state='waiting_for_api_budget', next_attempt_at=until.isoformat())


def deferred(status):
    value = status.get('next_attempt_at')
    if value and datetime.now(timezone.utc) < datetime.fromisoformat(value):
        return True
    status['next_attempt_at'] = None
    return False
