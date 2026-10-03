"""One durable all-open-item cohort on 100100; no age cutoff or balance checks."""
from collections import Counter
import json
import re

from operations import bacs_debtor_transfer as m, metorik_bacs_evidence as e
from operations import debtor_routing_policy as policy, routing_runtime as runtime

BATCH_SIZE = 100
HEADER = 'EntryID,Customer,YourRef,EntryNumber,Modified,Type,Reversal,Description,Journal,Currency,AmountFC,EntryDate'


def order_reference(header):
    ref = header.get('YourRef')
    if not isinstance(ref, str) or not re.fullmatch(r'TD[0-9]{4,10}', ref): return None
    if header.get('Reversal') is not False: return None
    if header.get('Type') == 20: return ref
    if header.get('Type') == 21:
        match = re.fullmatch(r'Order #(\d{4,10}) / Credit #(TD\d+)', header.get('Description') or '')
        if match and match[2] == ref: return 'TD' + match[1]
    return None


def candidates(rows, source):
    counts = Counter((r['EntryNumber'],r['YourRef'],r['JournalCode']) for r in rows)
    result = []
    for row in rows:
        m.require(row['AccountId'] == source, 'Open item outside source debtor')
        ref = row['YourRef']
        if (isinstance(ref,str) and re.fullmatch(r'TD[0-9]{4,10}',ref)
                and counts[(row['EntryNumber'],ref,row['JournalCode'])] == 1
                and m.amount(row['Amount']) != 0):
            result.append({'reference':ref,'entry_number':int(row['EntryNumber']),
                           'journal':str(row['JournalCode']).strip(),'remaining':str(row['Amount']),
                           'currency':row['CurrencyCode']})
    return sorted(result,key=lambda x:x['reference'])


async def debit_proof(api, credit, order, order_ref):
    """A credit needs its explicit original invoice, even if that invoice is paid."""
    rows = await api.rows('salesentry/SalesEntries', {
        '$filter': f"YourRef eq {m.quoted(order_ref)} and Type eq 20", '$select':HEADER})
    if len(rows) != 1: return None
    debit = rows[0]
    if (order.get('total') is None or order.get('total_refunds') is None
            or debit['YourRef'] != order_ref or debit['Type'] != 20 or debit['Reversal'] is not False
            or debit['Description'] != 'Order TD #' + order_ref[2:]
            or debit['Currency'] != credit['Currency'] or debit['Currency'] != order.get('currency')
            or m.amount(debit['AmountFC']) != m.amount(order.get('total'))
            or not (0 < -m.amount(credit['AmountFC']) <= m.amount(debit['AmountFC']))
            or m.amount(order.get('total_refunds')) < -m.amount(credit['AmountFC'])):
        return None
    return m.guid(debit['EntryID'])


def status(conn, saved):
    counts = dict(conn.execute("SELECT state,COUNT(*) FROM jnp_debtor_route_queue WHERE work_scope='cleanup' AND routing_policy=%s GROUP BY state",(policy.REVISION,)).fetchall())
    methods = {}
    for method,state,count in conn.execute("SELECT order_evidence->>'payment_method',state,COUNT(*) FROM jnp_debtor_route_queue WHERE work_scope='cleanup' AND routing_policy=%s GROUP BY order_evidence->>'payment_method',state",(policy.REVISION,)).fetchall():
        methods.setdefault(method or 'unknown',{})[state]=count
    return {'discovery_complete':saved['done'],'scanned':saved['cursor'],
            'source_items':saved['source_items'],'candidate_items':len(saved['items']),
            'selected':saved['selected'],'unclassified':len(saved['unclassified']),
            'counts':counts,'by_payment_method':methods}


async def discover_batch(api, conn, source):
    from operations.automatic_debtor_routing import STATUS
    saved = conn.execute('SELECT open_item_cleanup FROM jnp_debtor_route_control').fetchone()[0]
    if saved is None:
        rows = await api.rows('read/financial/ReceivablesList', {
            '$filter':f"AccountId eq guid'{m.guid(source)}'",
            '$select':'AccountId,YourRef,EntryNumber,Amount,JournalCode,CurrencyCode'})
        items = candidates(rows,source)
        saved = {'revision':policy.REVISION,'items':items,'cursor':0,'selected':0,
                 'source_items':len(rows),'unclassified':[], 'started_at':m.utcnow(),'done':False}
        conn.execute('UPDATE jnp_debtor_route_control SET open_item_cleanup=%s::jsonb',(json.dumps(saved),))
    m.require(saved['revision'] == policy.REVISION,'Cleanup policy changed')
    if saved['done']:
        STATUS['cleanup'] = status(conn,saved)
        return
    batch = saved['items'][saved['cursor']:saved['cursor']+BATCH_SIZE]
    headers = []
    numbers = sorted({i['entry_number'] for i in batch})
    for offset in range(0,len(numbers),25):
        filt = ' or '.join(f'EntryNumber eq {n}' for n in numbers[offset:offset+25])
        headers += await api.rows('salesentry/SalesEntries', {
            '$filter':f"Customer eq guid'{source}' and ({filt})",'$select':HEADER})
    mapped, unmatched = [], []
    for item in batch:
        hs = [h for h in headers if h['EntryNumber']==item['entry_number']
              and h['YourRef']==item['reference'] and h['Customer']==source
              and str(h['Journal']).strip()==item['journal'] and h['Currency']==item['currency']]
        h = hs[0] if len(hs)==1 else None
        ref = order_reference(h) if h else None
        if (ref is None or (h['Type']==20 and m.amount(item['remaining'])<=0)
                or (h['Type']==21 and m.amount(item['remaining'])>=0)):
            unmatched.append({**item,'reason':'Sales/order identity missing or ambiguous'})
            continue
        mapped.append((item,h,ref))
    refs = sorted({x[2] for x in mapped})
    proof = await e.lookup_orders(refs) if refs else {'orders':{}}
    inserts = []
    for item,h,ref in mapped:
        order = proof['orders'].get('#'+ref[2:])
        if not order or not order.get('payment_method'):
            unmatched.append({**item,'order_reference':ref,'reason':'Order payment method unavailable'})
            continue
        if order['payment_method'] not in policy.CLEANUP_ROUTES: continue
        m.require(order.get('order_number')=='#'+ref[2:] and type(order.get('order_id')) is int and order['order_id']>0,
                  'Invalid order evidence')
        debit_id = None
        if h['Type']==21:
            debit_id = await debit_proof(api,h,order,ref)
            if debit_id is None:
                unmatched.append({**item,'order_reference':ref,'reason':'Original debit booking not proven'})
                continue
        minimal = {k:order[k] for k in ('order_id','order_number','payment_method')}
        inserts.append((m.guid(h['EntryID']),item['reference'],h['Modified'],json.dumps(minimal),
                        ref,h['Type'],policy.REVISION,debit_id))
    with conn.transaction():
        for params in inserts:
            # Live header proves source 100100 again. A former completed route may
            # have been manually returned, as the user explicitly reported.
            # Uncertain writes are never replayed automatically.
            if conn.execute("SELECT EXISTS(SELECT 1 FROM jnp_debtor_route_backfill WHERE entry_id=%s AND state='uncertain')", (params[0],)).fetchone()[0]:
                unmatched.append({'reference':params[1],'reason':'Earlier write outcome is uncertain'})
                continue
            conn.execute("""INSERT INTO jnp_debtor_route_queue
                (entry_id,reference,modified,order_evidence,order_reference,entry_type,routing_policy,debit_entry_id,work_scope)
                VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,'cleanup')
                ON CONFLICT(entry_id) DO UPDATE SET modified=EXCLUDED.modified,
                order_evidence=EXCLUDED.order_evidence,order_reference=EXCLUDED.order_reference,
                entry_type=EXCLUDED.entry_type,routing_policy=EXCLUDED.routing_policy,
                debit_entry_id=EXCLUDED.debit_entry_id,work_scope='cleanup',
                state='pending',reason=NULL,next_check=NOW()
                WHERE jnp_debtor_route_queue.state<>'uncertain'
                  AND (jnp_debtor_route_queue.state NOT IN ('applied','verified')
                       OR jnp_debtor_route_queue.routing_policy IS DISTINCT FROM EXCLUDED.routing_policy)
                  AND jnp_debtor_route_queue.reference=EXCLUDED.reference
                  AND NOT EXISTS (SELECT 1 FROM jnp_debtor_route_backfill b
                                  WHERE b.entry_id=EXCLUDED.entry_id AND b.state='uncertain')""",params)
        saved['cursor'] += len(batch);saved['selected'] += len(inserts)
        saved['unclassified'] += unmatched
        saved['done'] = saved['cursor'] >= len(saved['items'])
        conn.execute('UPDATE jnp_debtor_route_control SET open_item_cleanup=%s::jsonb',(json.dumps(saved),))
    STATUS['cleanup'] = status(conn,saved)
    runtime.event('cleanup_discovery_progress',**STATUS['cleanup'])
