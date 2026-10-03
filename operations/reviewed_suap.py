"""Reconcile the operator's revised SUAP inventory with the existing live queue.

The workbook supplies order identities, never current balances or permission to
replay completed/uncertain writes. This module makes no Exact calls or DB writes.
"""
import json
from pathlib import Path

from operations import bacs_debtor_transfer as m

REVIEW = json.loads(Path(__file__).with_name('reviewed_suap_20261003.json').read_text())
METHOD = REVIEW['payment_method']
ENTRIES = {row['entry_id']: row for row in REVIEW['entries']}
m.require(len(ENTRIES) == len(REVIEW['entries']), 'Duplicate reviewed SUAP entry')


def validate_selection(selection):
    expected = ENTRIES.get(selection['entry_id'])
    m.require(expected is not None and selection['payment_method'] == METHOD
              and selection.get('work_scope') == 'cleanup'
              and all(selection.get(key) == expected[key]
                      for key in ('reference','order_reference','order_id','entry_type')),
              'SUAP selection differs from the operator reviewed inventory')


def reconcile(conn):
    rows = conn.execute("""SELECT entry_id::text,reference,
        COALESCE(order_reference,reference),order_evidence,entry_type,state,work_scope
        FROM jnp_debtor_route_queue WHERE work_scope='cleanup'
        AND (order_evidence->>'payment_method'=%s OR entry_id::text=ANY(%s))""",
        (METHOD,list(ENTRIES))).fetchall()
    saved = conn.execute('SELECT open_item_cleanup FROM jnp_debtor_route_control').fetchone()[0] or {}
    reasons = {item['reference']:item['reason'] for item in saved.get('unclassified',[])}
    matched, conflicts, seen = 0, [], set()
    for entry_id,reference,order_reference,order,entry_type,state,scope in rows:
        seen.add(entry_id)
        selection = {'entry_id':entry_id,'reference':reference,'order_reference':order_reference,
                     'order_id':(order or {}).get('order_id'),
                     'payment_method':(order or {}).get('payment_method'),
                     'entry_type':entry_type,'work_scope':scope}
        try: validate_selection(selection)
        except m.Stop: conflicts.append({'reference':reference,'state':state})
        else: matched += 1
    missing = [{'reference':row['reference'],'order_reference':row['order_reference'],
                'reason':reasons.get(row['reference'],'Not in the current validated cleanup queue')}
               for entry_id,row in ENTRIES.items() if entry_id not in seen]
    return {'source':REVIEW['source'],'source_version':REVIEW['source_version'],
            'reviewed_items':len(ENTRIES),'matched_queue_items':matched,
            'conflicts':conflicts,'not_queued':missing}
