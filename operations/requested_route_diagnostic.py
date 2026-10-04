"""Temporary operator-requested TD43893 diagnosis; GET/SELECT only, private logs.

Runs once per process before routing and expires on 5 October. It neither queues
work nor changes policy and never exposes an unauthenticated diagnostic endpoint.
"""
import asyncio
from datetime import datetime, timezone

from operations import allocation_connection as allocation
from operations import bacs_debtor_transfer as m
from operations import debtor_routing_policy as policy
from operations import metorik_bacs_evidence as evidence
from operations import routing_runtime as runtime

REFERENCE = 'TD43893'
EXPIRES = datetime(2026, 10, 5, tzinfo=timezone.utc)
FIELDS = ('EntryID', 'EntryNumber', 'YourRef', 'Customer', 'Created', 'Modified',
          'Status', 'Type', 'Reversal', 'PaymentCondition')


class ReadOnlyExact(m.Exact):
    async def request(self, method, url, params=None, payload=None):
        m.require(method == 'GET' and payload is None, 'Diagnostic is read-only')
        return await super().request(method, url, params=params)


async def collect(app):
    with app._db_connect() as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            control = conn.execute('''SELECT enabled,started_at,cursor_at,last_scan,
                routing_policy FROM jnp_debtor_route_control''').fetchone()
            queue = conn.execute('''SELECT entry_id::text,reference,modified,state,
                reason,next_check,work_scope,order_reference,
                order_evidence->>'payment_method' FROM jnp_debtor_route_queue
                WHERE reference=%s OR order_reference=%s''',
                (REFERENCE, REFERENCE)).fetchall()
    runtime.event('requested_route_diagnostic_queue', reference=REFERENCE,
        control=dict(zip(('enabled','started_at','cursor_at','last_scan','policy'), control)),
        entries=[dict(zip(('entry_id','reference','modified','state','reason',
                          'next_check','scope','order_reference','stored_method'), r))
                 for r in queue])
    api = ReadOnlyExact(allocation.RoutingApp(app))
    rows = await api.rows('salesentry/SalesEntries', {
        '$filter': "YourRef eq 'TD43893'", '$select': ','.join(FIELDS)})
    runtime.event('requested_route_diagnostic_exact', reference=REFERENCE,
        entries=[{k: row.get(k) for k in FIELDS} for row in rows], limits=api.limits)
    proof = await evidence.lookup_orders([REFERENCE])
    order = proof['orders'].get('#43893') or {}
    method = order.get('payment_method')
    runtime.event('requested_route_diagnostic_order', reference=REFERENCE,
        order={k: order.get(k) for k in ('order_id','order_number','payment_method')},
        continuous_destination=policy.CONTINUOUS_ROUTES.get(method),
        cleanup_destination=policy.CLEANUP_ROUTES.get(method),
        retain_on_source=method in policy.RETAIN_ON_SOURCE)


async def run(app, now=None):
    if (now or datetime.now(timezone.utc)) >= EXPIRES:
        return
    try:
        await asyncio.wait_for(collect(app), timeout=75)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        runtime.event('requested_route_diagnostic_failed', reference=REFERENCE,
            error_type=type(exc).__name__,
            http_status=exc.status_code if isinstance(exc, m.ExactRequestError) else None)
