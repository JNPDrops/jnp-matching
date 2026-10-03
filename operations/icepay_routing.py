"""Authorized ICEPAY catch-up from all current open items, without a date cut-off.

Uses the existing database and worker. No public endpoint or new infrastructure.
Only a proven ICEPAY order can enter this cohort; execution rechecks the current
debtor and remaining amount and changes Customer only.
"""
from collections import Counter
import json
import re

from operations import bacs_debtor_transfer as m, metorik_bacs_evidence as e
from operations import routing_runtime as runtime

METHOD = 'icepay-ideal'
DESTINATION = '109419'
ACTIVE_METHODS = frozenset({METHOD})
BATCH_SIZE = 100


def initialize(conn):
    conn.execute('ALTER TABLE jnp_debtor_route_control ADD COLUMN IF NOT EXISTS icepay_enabled_at TIMESTAMPTZ')
    conn.execute('ALTER TABLE jnp_debtor_route_control ADD COLUMN IF NOT EXISTS icepay_discovery JSONB')
    conn.execute('ALTER TABLE jnp_debtor_route_queue ADD COLUMN IF NOT EXISTS order_evidence JSONB')


def resume_once(conn):
    """3 October user authorization applies to ICEPAY only; later pauses persist."""
    with conn.transaction():
        row = conn.execute('SELECT started_at,cursor_at,icepay_enabled_at FROM jnp_debtor_route_control FOR UPDATE').fetchone()
        m.require(row is not None and row[0] is not None and row[1] is not None,
                  'Previously initialized routing worker required')
        if row[2] is not None:
            return
        conn.execute('UPDATE jnp_debtor_route_control SET enabled=TRUE,pause_reason=NULL,icepay_enabled_at=NOW()')
    runtime.event('authorized_icepay_resume', payment_method=METHOD, destination=DESTINATION)


def candidates(rows, source):
    """Exclude ambiguous/non-order references; never guess a payment method."""
    refs = Counter(r['YourRef'] for r in rows)
    selected = []
    for r in rows:
        m.require(r['AccountId'] == source, 'Open item outside source debtor')
        ref = r['YourRef']
        if (isinstance(ref, str) and re.fullmatch(r'TD[0-9]{4,10}', ref)
                and refs[ref] == 1 and m.amount(r['Amount']) > 0):
            selected.append({'reference': ref, 'entry_number': int(r['EntryNumber'])})
    return sorted(selected, key=lambda r: r['reference'])


async def discover_batch(api, conn, source):
    """Persist progress so restarts never discard the older open-item population."""
    from operations.automatic_debtor_routing import STATUS
    saved = conn.execute('SELECT icepay_discovery FROM jnp_debtor_route_control').fetchone()[0]
    if saved is None:
        rows = await api.rows('read/financial/ReceivablesList', {
            '$filter': f"AccountId eq guid'{m.guid(source)}'",
            '$select': 'AccountId,YourRef,EntryNumber,Amount'})
        saved = {'items': candidates(rows, source), 'cursor': 0, 'queued': 0,
                 'review': 0, 'started_at': m.utcnow(), 'done': False}
        conn.execute('UPDATE jnp_debtor_route_control SET icepay_discovery=%s::jsonb', (json.dumps(saved),))
    if saved['done']:
        STATUS['icepay_discovery'] = {k: saved[k] for k in ('cursor', 'queued', 'review', 'done')}
        return
    batch = saved['items'][saved['cursor']:saved['cursor'] + BATCH_SIZE]
    proof = await e.lookup_orders([r['reference'] for r in batch]) if batch else {'orders': {}}
    inserts = []
    review = 0
    for item in batch:
        ref = item['reference']
        order = proof['orders'].get('#' + ref[2:])
        if order is None:
            review += 1
            continue
        if order['payment_method'] != METHOD:
            continue
        m.require(order.get('order_number') == '#' + ref[2:] and type(order.get('order_id')) is int
                  and order['order_id'] > 0, 'Invalid ICEPAY order evidence')
        rows = await api.rows('salesentry/SalesEntries', {
            '$filter': f"Customer eq guid'{source}' and YourRef eq {m.quoted(ref)}",
            '$select': 'EntryID,Customer,YourRef,EntryNumber,Modified,Type,Reversal'})
        if (len(rows) != 1 or rows[0]['Customer'] != source or rows[0]['YourRef'] != ref
                or rows[0]['EntryNumber'] != item['entry_number'] or rows[0]['Type'] != 20
                or rows[0]['Reversal'] is not False):
            review += 1
            continue
        row = rows[0]
        inserts.append((m.guid(row['EntryID']), ref, row['Modified'], json.dumps(order)))
    with conn.transaction():
        for params in inserts:
            conn.execute("""INSERT INTO jnp_debtor_route_queue(entry_id,reference,modified,order_evidence)
                VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT(entry_id) DO UPDATE
                SET order_evidence=EXCLUDED.order_evidence,state='pending',reason=NULL,next_check=NOW()
                WHERE jnp_debtor_route_queue.state NOT IN ('verified','applied','uncertain')
                  AND jnp_debtor_route_queue.reference=EXCLUDED.reference""", params)
        saved.update(cursor=saved['cursor'] + len(batch), queued=saved['queued'] + len(inserts),
                     review=saved['review'] + review)
        saved['done'] = saved['cursor'] >= len(saved['items'])
        conn.execute('UPDATE jnp_debtor_route_control SET icepay_discovery=%s::jsonb', (json.dumps(saved),))
    STATUS['icepay_discovery'] = {k: saved[k] for k in ('cursor', 'queued', 'review', 'done')}
    runtime.event('icepay_discovery_progress', **STATUS['icepay_discovery'])
