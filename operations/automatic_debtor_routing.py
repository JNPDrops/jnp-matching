"""Authorized debtor-only routing and a durable open-item cleanup.

Uses the existing service/database. Unknown mappings and uncertain writes stay
isolated. No public write route, matching, reimport, or balance comparisons.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import re
from uuid import uuid4

from operations import bacs_debtor_transfer as m, metorik_bacs_evidence as e
from operations import customer_only_routing as customer_only
from operations import routing_runtime as runtime
from operations import debtor_routing_policy as policy, open_item_cleanup as cleanup
from operations import allocation_connection as allocation
from operations import reviewed_suap

LOCK_ID = 3977752100100
INTERVAL = 300
# User authorized the fixed policy on 2026-10-03 at 20:55 Amsterdam.
OPERATOR_PAUSED = False
ACTIVE_METHODS = frozenset(policy.CONTINUOUS_ROUTES)
STATUS = {"enabled": False, "state": "not_started", "last_scan": None,
          "mode": "customer_only", "balance_checks": False, "applied_since_start": 0,
          "interval_seconds": INTERVAL, "last_error": None, "next_attempt_at": None,
          "retained_payment_methods": dict(policy.RETAIN_ON_SOURCE),
          "active_routes": dict(policy.CONTINUOUS_ROUTES),
          "cleanup_routes": dict(policy.CLEANUP_ROUTES), "cleanup": None,
          "policy_revision": policy.REVISION, "api_limits": None,
          "execution_connection": "JNP Allocation", "automatic_key_switching": False,
          "immediate_cleanup_routes": dict(policy.IMMEDIATE_CLEANUP_ROUTES),
          "current_phase": "immediate_cleanup" if policy.before_scheduled_start() else "all_routes",
          "paused_payment_methods": []}


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_debtor_route_control (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        started_at TIMESTAMPTZ, cursor_at TIMESTAMPTZ,
        pause_reason TEXT, last_scan TIMESTAMPTZ)""")
    conn.execute("INSERT INTO jnp_debtor_route_control(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING")
    conn.execute('ALTER TABLE jnp_debtor_route_control ADD COLUMN IF NOT EXISTS continuous_routing_recovered_at TIMESTAMPTZ')
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_debtor_route_queue (
        entry_id UUID PRIMARY KEY, reference TEXT NOT NULL, modified TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', reason TEXT,
        next_check TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_debtor_route_audit (
        id BIGSERIAL PRIMARY KEY, run_id UUID NOT NULL, entry_id UUID NOT NULL,
        event TEXT NOT NULL, body JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def pause(conn, reason):
    policy.initialize(conn)
    conn.execute("UPDATE jnp_debtor_route_control SET enabled=FALSE,pause_reason=%s,routing_policy=%s", (reason,policy.REVISION))
    STATUS.update(enabled=False, state="paused")


class AutomaticExact(m.Exact):
    def __init__(self, app_module, conn):
        super().__init__(allocation.RoutingApp(app_module))
        self.conn = conn

    async def request(self, method, url, params=None, payload=None):
        if type(self.limits.get('remaining')) is int and not customer_only.budget_available(self, calls=1):
            raise customer_only.BudgetDeferred('Waiting for API budget')
        before=self.limits
        try:
            return await super().request(method, url, params=params, payload=payload)
        finally:
            if self.limits is not before:
                STATUS.update(api_limits=dict(self.limits),api_limits_checked_at=m.utcnow())

    async def change_customer(self, entry_id, destination_id):
        if OPERATOR_PAUSED:
            raise m.WritePaused('Debtor routing paused by operator')
        # A pause takes effect even when an earlier read-only preflight is still
        # running. Failure to read the switch blocks the write as well.
        enabled = self.conn.execute('SELECT enabled,routing_policy FROM jnp_debtor_route_control').fetchone()
        if enabled != (True, policy.REVISION):
            raise m.WritePaused('Automatic routing was paused before the write')
        accounts = await customer_only.route_accounts(self, methods=policy.routes_for_now())
        m.require(destination_id in {accounts[code] for code in policy.routes_for_now().values()},
                  'Destination is outside the authorized policy')
        if policy.before_scheduled_start():
            entry = self.conn.execute("SELECT work_scope,order_evidence->>'payment_method' FROM jnp_debtor_route_queue WHERE entry_id=%s", (m.guid(entry_id),)).fetchone()
            if (entry is None or entry[0] != 'cleanup'
                    or entry[1] not in policy.IMMEDIATE_CLEANUP_ROUTES
                    or destination_id != accounts[policy.IMMEDIATE_CLEANUP_ROUTES[entry[1]]]):
                raise m.WritePaused('Only authorized historical routes may run before the scheduled start')
        await super().change_customer(entry_id, destination_id)


class Audit:
    def __init__(self, conn, entry_id):
        self.conn, self.entry_id, self.run_id = conn, entry_id, str(uuid4())
        self.write_started = False

    def persist_event(self, event):
        body = dict(event)
        if 'rows' in body:
            rows = body.pop('rows')
            totals = {}
            for row in rows:
                key = row['AccountCode'].strip() + '/' + row['CurrencyCode']
                totals[key] = str(m.amount(totals.get(key, 0)) + m.amount(row['Amount']))
            body.update(row_count=len(rows), population_sha256=m.digest(rows), totals=totals)
        with self.conn.transaction():
            self.conn.execute("INSERT INTO jnp_debtor_route_audit(run_id,entry_id,event,body) VALUES(%s,%s,%s,%s::jsonb)",
                              (self.run_id, self.entry_id, event['event'], json.dumps(body, default=str)))
            if event['event'] == 'write_intent':
                self.conn.execute("UPDATE jnp_debtor_route_queue SET state='uncertain',reason='write intent persisted' WHERE entry_id=%s", (self.entry_id,))
            elif event['event'] == 'complete':
                self.conn.execute("UPDATE jnp_debtor_route_queue SET state='verified',reason=NULL WHERE entry_id=%s", (self.entry_id,))
            elif event['event'] == 'customer_applied':
                self.conn.execute("UPDATE jnp_debtor_route_queue SET state='applied',reason=NULL WHERE entry_id=%s", (self.entry_id,))
            elif event['event'] == 'entry_failure':
                self.conn.execute("UPDATE jnp_debtor_route_queue SET state=%s,reason=%s,next_check=NOW()+INTERVAL '10 minutes' WHERE entry_id=%s",
                                  (event['state'],event['reason'],self.entry_id))
        if event['event'] == 'write_intent':
            self.write_started = True


def exact_time(value):
    match = re.fullmatch(r'/Date\((-?\d+)\)/', value or '')
    m.require(match is not None, 'Invalid Exact creation timestamp')
    return datetime.fromtimestamp(int(match[1])/1000, timezone.utc)


def scan_params(source_id, started_at, cursor_at, end):
    def stamp(dt):
        m.require(dt.tzinfo is not None, 'Naive scan timestamp')
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    lower = max(started_at, cursor_at - timedelta(minutes=2))
    return {'$filter': f"Customer eq guid'{m.guid(source_id)}' and Modified ge datetime'{stamp(lower)}' and Modified lt datetime'{stamp(end)}'",
            '$select': 'EntryID,Customer,Created,Modified,YourRef,Description,Type,Reversal'}


def enqueue(conn, rows, source_id, started_at, end):
    m.require(len({r['EntryID'] for r in rows}) == len(rows), 'Duplicate incremental Exact entry')
    with conn.transaction():
        for row in rows:
            m.require(row['Customer'] == source_id,
                      'Incremental query returned an out-of-scope entry')
            ref = row['YourRef']
            if (row['Type'] != 20 or row['Reversal'] is not False or not isinstance(ref,str)
                    or not re.fullmatch(r'TD[0-9]{4,10}', ref)):
                continue
            conn.execute("""INSERT INTO jnp_debtor_route_queue
                (entry_id,reference,modified,order_reference,routing_policy)
                SELECT %s,%s,%s,%s,%s WHERE NOT EXISTS
                  (SELECT 1 FROM jnp_debtor_route_backfill WHERE entry_id=%s AND state='uncertain')
                ON CONFLICT(entry_id) DO UPDATE
                SET reference=EXCLUDED.reference,modified=EXCLUDED.modified,state='pending',reason=NULL,
                    order_evidence=NULL,order_reference=EXCLUDED.reference,entry_type=20,
                    routing_policy=EXCLUDED.routing_policy,next_check=NOW()
                WHERE jnp_debtor_route_queue.state<>'uncertain'
                  AND jnp_debtor_route_queue.modified<>EXCLUDED.modified
                  AND (jnp_debtor_route_queue.state NOT IN ('verified','applied')
                       OR jnp_debtor_route_queue.work_scope='continuous')""",
                         (m.guid(row['EntryID']),ref,row['Modified'],ref,policy.REVISION,m.guid(row['EntryID'])))
        conn.execute("UPDATE jnp_debtor_route_control SET cursor_at=%s,last_scan=NOW()", (end,))


def record_state(conn, entry_id, state, reason):
    conn.execute("UPDATE jnp_debtor_route_queue SET state=%s,reason=%s,next_check=NOW()+INTERVAL '10 minutes' WHERE entry_id=%s",
                 (state,reason,entry_id))


async def process_entry(api, conn, entry_id, reference, order, *, work_scope='continuous',
                        order_reference=None, entry_type=20, debit_entry_id=None):
    method = order.get('payment_method')
    if method in policy.RETAIN_ON_SOURCE:
        record_state(conn,entry_id,'retained','Fibonatix stays on 100100 by operator instruction')
        return True
    if method not in policy.routes(work_scope):
        record_state(conn,entry_id,'skipped','Payment method outside this work scope')
        return True
    if not policy.work_allowed(method,work_scope):
        conn.execute("UPDATE jnp_debtor_route_queue SET state='pending',reason='Waiting for scheduled route start',next_check=%s WHERE entry_id=%s",(policy.START_AT,entry_id))
        return True
    audit = Audit(conn,entry_id)
    try:
        order_reference = order_reference or reference
        m.require(order.get('order_number') == '#' + order_reference[2:], 'Order reference mismatch')
        selection = {'entry_id':entry_id,'reference':reference,'order_id':order['order_id'],
                     'payment_method':method,'work_scope':work_scope,
                     'order_reference':order_reference,'entry_type':entry_type,
                     'debit_entry_id':debit_entry_id}
        if method == reviewed_suap.METHOD:
            reviewed_suap.validate_selection(selection)
        result = await customer_only.change_selected(api,selection,
                    await customer_only.route_accounts(api, methods=policy.routes_for_now()),audit)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result = customer_only.failure_result(exc,audit.write_started)
        audit.persist_event({'event':'entry_failure','entry_id':entry_id,**result})
        runtime.event('entry_isolated',queue=work_scope,entry_id=entry_id,**result)
        STATUS['last_error'] = result['reason']
        if isinstance(exc,customer_only.BudgetDeferred) or (isinstance(exc,m.ExactRequestError) and exc.status_code == 429):
            runtime.defer_until_reset(STATUS,api.limits)
        return not result['stop_cycle']
    record_state(conn,entry_id,result['state'],result['reason'])
    if result.get('confirmation') == 'Exact HTTP acknowledgement':
        STATUS['applied_since_start'] += 1
        runtime.event('customer_applied',entry_id=entry_id,reference=reference,
                      payment_method=method,destination=result['destination'],work_scope=work_scope)
    if result['state']=='pending' and result['reason']=='Waiting for API budget':
        runtime.defer_until_reset(STATUS,api.limits)
        return False
    return True


async def validate_routes(api):
    return (await customer_only.route_accounts(api, methods=policy.routes_for_now()))[m.SOURCE]


async def cycle(app_module):
    m.require(bool(app_module.DATABASE_URL), 'Persistent database required for automatic routing')
    api = None
    with app_module._db_connect() as conn:
        initialize(conn)
        from operations import backfill_debtor_routing as backfill
        backfill.initialize(conn)
        policy.initialize(conn)
        if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,)).fetchone()[0]:
            STATUS['state'] = 'another_runner'
            return
        try:
            with Path('/tmp/jnp-bacs-debtor-transfer.lock').open('a') as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    STATUS['state']='manual_run_active'
                    return
                policy.activate_once(conn)
                enabled,started,cursor,last_scan = conn.execute('SELECT enabled,started_at,cursor_at,last_scan FROM jnp_debtor_route_control').fetchone()
                STATUS['enabled'] = enabled
                if not enabled:
                    STATUS.update(state='disabled',next_attempt_at=None)
                    return
                early=policy.before_scheduled_start()
                STATUS['current_phase']='immediate_cleanup' if early else 'all_routes'
                if early and STATUS['state']=='immediate_cleanup_complete': return
                if runtime.deferred(STATUS):
                    STATUS['state']='waiting_for_api_budget'
                    return
                STATUS['last_error'] = None
                api=AutomaticExact(app_module,conn)
                STATUS['state']='reading_current_open_items'
                previous = STATUS.get('api_limits') or {}
                reset = previous.get('reset_ms')
                # Reuse budget knowledge while draining the queue. A reset allows
                # the next real request to obtain fresh response headers.
                if type(reset) is int and reset > datetime.now(timezone.utc).timestamp()*1000:
                    api.limits = dict(previous)
                source=await validate_routes(api)
                now=datetime.now(timezone.utc).replace(microsecond=0)
                end=now-timedelta(seconds=60)
                # Fast cleanup cycles must not repeatedly rescan all new entries.
                if end>cursor and (last_scan is None or (now-last_scan).total_seconds()>=INTERVAL or not api.limits):
                    rows=await api.rows('salesentry/SalesEntries',scan_params(source,started,cursor,end))
                    enqueue(conn,rows,source,started,end)
                    STATUS['last_scan']=m.utcnow()
                await cleanup.discover_batch(api,conn,source)
                if 'suap_reassessment' not in STATUS:
                    STATUS['suap_reassessment']=reviewed_suap.reconcile(conn)
                    runtime.event('suap_reassessment',**STATUS['suap_reassessment'])
                STATUS['state']='routing_immediate_cleanup' if early else 'processing_queue'
                if not customer_only.budget_available(api):
                    runtime.defer_until_reset(STATUS,api.limits)
                    return
                queued=conn.execute("""SELECT q.entry_id::text,q.reference,q.order_evidence,
                    COALESCE(q.order_reference,q.reference),q.entry_type,q.work_scope,q.debit_entry_id::text
                    FROM jnp_debtor_route_queue q WHERE state='pending' AND next_check<=NOW()
                    AND (%s OR (q.work_scope='cleanup' AND q.order_evidence->>'payment_method'=ANY(%s)))
                    AND NOT EXISTS (SELECT 1 FROM jnp_debtor_route_backfill b WHERE b.entry_id=q.entry_id AND b.state='uncertain')
                    ORDER BY (work_scope='continuous') DESC,(order_evidence IS NOT NULL) DESC,next_check,entry_id LIMIT 50""",(not early,list(policy.IMMEDIATE_CLEANUP_ROUTES))).fetchall()
                if queued:
                    references=sorted({r[3] for r in queued if r[2] is None})
                    proof=await e.lookup_orders(references) if references else {'orders':{}}
                    for entry_id,reference,stored_order,order_ref,entry_type,scope,debit_id in queued:
                        order=stored_order or proof['orders'].get('#'+order_ref[2:])
                        if order is None:
                            record_state(conn,entry_id,'pending','Order not yet present in Metorik; no inference')
                            continue
                        if not customer_only.budget_available(api):
                            runtime.defer_until_reset(STATUS,api.limits)
                            return
                        if await process_entry(api,conn,entry_id,reference,order,work_scope=scope,
                                order_reference=order_ref,entry_type=entry_type,debit_entry_id=debit_id) is False:
                            if STATUS['state'] != 'waiting_for_api_budget': STATUS['state']='retry_next_cycle'
                            return
                # Retire legacy Fibonatix work locally. This path makes no Exact
                # request because Fibonatix remains on the source debtor.
                await backfill.process_pending(api,conn)
                saved=conn.execute('SELECT open_item_cleanup FROM jnp_debtor_route_control').fetchone()[0]
                STATUS['cleanup']=cleanup.status(conn,saved)
                ready=conn.execute("SELECT EXISTS(SELECT 1 FROM jnp_debtor_route_queue WHERE state='pending' AND (%s OR (work_scope='cleanup' AND order_evidence->>'payment_method'=ANY(%s))))",(not early,list(policy.IMMEDIATE_CLEANUP_ROUTES))).fetchone()[0]
                STATUS['state']='processing_queue' if ready or not saved['done'] else 'watching'
                if early and not ready and saved['done']:
                    STATUS.update(state='immediate_cleanup_complete',next_attempt_at=policy.START_AT.isoformat())
                runtime.event('cycle_complete',applied_since_start=STATUS['applied_since_start'],
                              last_scan=STATUS['last_scan'],cleanup=STATUS['cleanup'],queues=runtime.queue_counts(conn))
        except customer_only.BudgetDeferred:
            runtime.defer_until_reset(STATUS,api.limits if api else {})
            raise
        finally:
            if api is not None: STATUS['api_limits']=dict(api.limits)
            conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_ID,))


def sleep_seconds(status, now=None):
    now = now or datetime.now(timezone.utc)
    next_at=status.get('next_attempt_at')
    if next_at:
        return max(1,min(INTERVAL,(datetime.fromisoformat(next_at)-now).total_seconds()))
    return 15 if status.get('state')=='processing_queue' else INTERVAL


async def serve(app_module):
    while True:
        try:
            if OPERATOR_PAUSED:
                # A retired paused instance must not overwrite newer policy
                # activation during a rolling deployment.
                STATUS.update(enabled=False,state='paused',next_attempt_at=None)
            else:
                await cycle(app_module)
        except asyncio.CancelledError:
            raise
        except customer_only.BudgetDeferred:
            runtime.event('cycle_deferred',reason='Waiting for API budget')
        except Exception as exc:
            STATUS['state']='retry_next_cycle'
            STATUS['last_error'] = (f'Exact {exc.method} HTTP {exc.status_code}'
                                   if isinstance(exc,m.ExactRequestError) else
                                   'Existing route debtor missing or ambiguous'
                                   if isinstance(exc,m.Stop) and str(exc) == 'Existing route debtor missing or ambiguous'
                                   else 'Routing cycle failed; retry pending')
            if isinstance(exc,m.ExactRequestError) and exc.status_code == 429:
                runtime.defer_until_reset(STATUS,exc.limits)
            runtime.event('cycle_deferred',reason=STATUS['last_error'])
        await asyncio.sleep(sleep_seconds(STATUS))


async def enable(app_module, since):
    m.require(since.tzinfo is not None, 'Activation timestamp must include timezone')
    since=since.astimezone(timezone.utc).replace(microsecond=0)
    m.require(timedelta(0) <= datetime.now(timezone.utc)-since <= timedelta(hours=1),
              'Activation must cover only the last hour/new entries')
    api=m.Exact(app_module)
    await validate_routes(api)
    # Harmless store/order lookup proves the connected Metorik reader works.
    await e.lookup_orders(['TD48517'])
    with app_module._db_connect() as conn:
        initialize(conn)
        m.require(conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,)).fetchone()[0], 'Another routing process is running')
        try:
            m.require(not conn.execute("SELECT EXISTS(SELECT 1 FROM jnp_debtor_route_queue WHERE state='uncertain')").fetchone()[0],
                      'Unresolved write intent; do not enable')
            previous=conn.execute('SELECT started_at FROM jnp_debtor_route_control').fetchone()[0]
            m.require(previous is None, 'Already initialized; preserve cursor and use reviewed recovery')
            # Prove the exact incremental query before enabling any automation.
            await api.rows('salesentry/SalesEntries',scan_params('ec2af99c-809c-40e3-9057-8a31962ae1cf',since,since,datetime.now(timezone.utc)))
            conn.execute('UPDATE jnp_debtor_route_control SET enabled=TRUE,started_at=%s,cursor_at=%s,pause_reason=NULL',(since,since))
        finally: conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_ID,))
    return {'enabled':True,'since':since.isoformat(),'interval_seconds':INTERVAL,'routes':{k:v[0] for k,v in m.ROUTES.items()}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('enable','status','pause','run-once'))
    parser.add_argument('--since')
    args=parser.parse_args()
    from app import main as app_module
    try:
        if args.action=='enable':
            m.require(bool(args.since),'Provide an explicit new-entry activation time')
            result=asyncio.run(enable(app_module,datetime.fromisoformat(args.since)))
        elif args.action=='run-once':
            asyncio.run(cycle(app_module));result=STATUS
        else:
            with app_module._db_connect() as conn:
                initialize(conn)
                if args.action=='pause':pause(conn,'Paused by authorized operator')
                row=conn.execute('SELECT enabled,started_at,cursor_at,pause_reason,last_scan FROM jnp_debtor_route_control').fetchone()
                counts=conn.execute('SELECT state,COUNT(*) FROM jnp_debtor_route_queue GROUP BY state').fetchall()
                result={'enabled':row[0],'started_at':row[1],'cursor_at':row[2],'pause_reason':row[3],'last_scan':row[4],'counts':dict(counts)}
        print(json.dumps(result,default=str))
    except Exception as exc:
        parser.exit(2,(str(exc) if isinstance(exc,m.Stop) else 'Automatic routing stopped; inspect private audit. Details suppressed.')+'\n')


if __name__=='__main__':main()
