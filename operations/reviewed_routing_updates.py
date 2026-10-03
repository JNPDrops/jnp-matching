"""Apply the revised inventory's changed/unknown identities to live routing.

Existing live proof remains authoritative for orders absent from the older
snapshot. Known conflicts and explicitly unknown payment methods block a write.
"""
import json
from pathlib import Path

from operations import bacs_debtor_transfer as m, debtor_routing_policy as p

REVIEW=json.loads(Path(__file__).with_name('reviewed_routing_updates_20261003.json').read_text())
ENTRIES={row['entry_id']:row for row in REVIEW['entries']}
m.require(len(ENTRIES)==len(REVIEW['entries']),'Duplicate reviewed routing entry')


def validate_selection(selection):
    expected=ENTRIES.get(selection['entry_id'])
    if expected is None: return
    m.require(expected['payment_method'] is not None
              and all(selection.get(key)==expected[key] for key in
                      ('reference','order_reference','order_id','payment_method','entry_type')),
              'Routing identity conflicts with the revised inventory')


def reconcile(conn):
    rows=conn.execute("""SELECT entry_id::text,reference,
        COALESCE(order_reference,reference),order_evidence,entry_type,state
        FROM jnp_debtor_route_queue WHERE entry_id::text=ANY(%s)""",(list(ENTRIES),)).fetchall()
    saved=conn.execute('SELECT open_item_cleanup FROM jnp_debtor_route_control').fetchone()[0] or {}
    reasons={x['reference']:x['reason'] for x in saved.get('unclassified',[])}
    seen,conflicts,matched=set(),[],0
    for entry_id,reference,order_reference,order,entry_type,state in rows:
        seen.add(entry_id)
        selection={'entry_id':entry_id,'reference':reference,'order_reference':order_reference,
                   'order_id':(order or {}).get('order_id'),
                   'payment_method':(order or {}).get('payment_method'),'entry_type':entry_type}
        try:validate_selection(selection)
        except m.Stop:conflicts.append({'reference':reference,'state':state})
        else:matched+=1
    missing=[{'reference':x['reference'],'payment_method':x['payment_method'],
              'reason':reasons.get(x['reference'],'Not in the current validated cleanup queue')}
             for key,x in ENTRIES.items() if key not in seen and x['payment_method'] in p.CLEANUP_ROUTES]
    return {'source':REVIEW['source'],'source_version':REVIEW['source_version'],
            'reviewed_changed_or_unknown':len(ENTRIES),'matched_queue_items':matched,
            'conflicts':conflicts,'not_queued_authorized_methods':missing}
