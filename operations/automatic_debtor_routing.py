"""Authorized bacs/Plisio/Fibonatix routing in the existing service and database.

Disabled until the operator enables the persistent control row. Polls new Exact
sales entries; no public write route, new infrastructure, matching or reimport.
A durable write intent without a complete audit halts automation after a restart.
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

LOCK_ID = 3977752100100
INTERVAL = 300
STATUS = {"enabled": False, "state": "not_started", "last_scan": None}


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_debtor_route_control (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        started_at TIMESTAMPTZ, cursor_at TIMESTAMPTZ,
        pause_reason TEXT, last_scan TIMESTAMPTZ)""")
    conn.execute("INSERT INTO jnp_debtor_route_control(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING")
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_debtor_route_queue (
        entry_id UUID PRIMARY KEY, reference TEXT NOT NULL, modified TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', reason TEXT,
        next_check TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_debtor_route_audit (
        id BIGSERIAL PRIMARY KEY, run_id UUID NOT NULL, entry_id UUID NOT NULL,
        event TEXT NOT NULL, body JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def pause(conn, reason):
    conn.execute("UPDATE jnp_debtor_route_control SET enabled=FALSE,pause_reason=%s", (reason,))
    STATUS.update(enabled=False, state="paused")


class AutomaticExact(m.Exact):
    def __init__(self, app_module, conn):
        super().__init__(app_module)
        self.conn = conn

    async def change_customer(self, entry_id, destination_id):
        # A pause takes effect even when an earlier read-only preflight is still
        # running. Failure to read the switch blocks the write as well.
        enabled = self.conn.execute('SELECT enabled FROM jnp_debtor_route_control').fetchone()
        m.require(enabled == (True,), 'Automatic routing was paused before the write')
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
    return {'$filter': f"Customer eq guid'{m.guid(source_id)}' and Created ge datetime'{stamp(started_at)}' and Modified ge datetime'{stamp(lower)}' and Modified lt datetime'{stamp(end)}'",
            '$select': 'EntryID,Customer,Created,Modified,YourRef,Description,Type,Reversal'}


def enqueue(conn, rows, source_id, started_at, end):
    m.require(len({r['EntryID'] for r in rows}) == len(rows), 'Duplicate incremental Exact entry')
    with conn.transaction():
        for row in rows:
            m.require(row['Customer'] == source_id and exact_time(row['Created']) >= started_at.replace(microsecond=0),
                      'Incremental query returned an out-of-scope entry')
            ref = row['YourRef']
            if (row['Type'] != 20 or row['Reversal'] is not False or not isinstance(ref,str)
                    or not re.fullmatch(r'TD[0-9]{4,10}', ref)
                    or row['Description'] != 'Order TD #' + ref[2:]):
                continue
            conn.execute("""INSERT INTO jnp_debtor_route_queue(entry_id,reference,modified)
                VALUES(%s,%s,%s) ON CONFLICT(entry_id) DO UPDATE
                SET reference=EXCLUDED.reference,modified=EXCLUDED.modified,state='pending',reason=NULL,next_check=NOW()
                WHERE jnp_debtor_route_queue.state NOT IN ('verified','uncertain')
                  AND jnp_debtor_route_queue.modified<>EXCLUDED.modified""", (m.guid(row['EntryID']),ref,row['Modified']))
        conn.execute("UPDATE jnp_debtor_route_control SET cursor_at=%s,last_scan=NOW()", (end,))


def record_state(conn, entry_id, state, reason):
    conn.execute("UPDATE jnp_debtor_route_queue SET state=%s,reason=%s,next_check=NOW()+INTERVAL '10 minutes' WHERE entry_id=%s",
                 (state,reason,entry_id))


async def process_entry(api, conn, entry_id, reference, order):
    method = order['payment_method']
    if method not in m.ROUTES:
        record_state(conn,entry_id,'skipped','Other webshop payment method')
        return
    manifest = [{'entry_id':entry_id,'reference':reference,'order_id':order['order_id']}]
    p = await e.evidence_plan(api,manifest,method)
    if p['review']:
        record_state(conn,entry_id,'review',p['review'][0]['reason'])
        return
    if not p['eligible']:
        record_state(conn,entry_id,'skipped','No remaining open sales entry')
        return
    audit = Audit(conn,entry_id)
    try:
        # Identical Customer-only executor and preflight/global verification as
        # the approved one-shot correction. No alternate mutation path exists.
        await m.apply(api,p,p['plan_sha256'],audit,accept_derived_changes=True)
    except BaseException:
        if audit.write_started:
            pause(conn,'Write attempted without a complete verified audit; inspect durable audit')
        raise


async def validate_routes(api):
    contexts = [await m.context(api, method) for method in m.ROUTES]
    sources = {ctx['accounts'][m.SOURCE]['ID'] for ctx in contexts}
    m.require(len(sources) == 1, 'Route source debtors disagree')
    return sources.pop()


async def cycle(app_module):
    m.require(bool(app_module.DATABASE_URL), 'Persistent database required for automatic routing')
    with app_module._db_connect() as conn:
        initialize(conn)
        if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,)).fetchone()[0]:
            STATUS['state'] = 'another_runner'
            return
        try:
            with Path('/tmp/jnp-bacs-debtor-transfer.lock').open('a') as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    STATUS['state']='manual_run_active'
                    return
                enabled,started,cursor = conn.execute('SELECT enabled,started_at,cursor_at FROM jnp_debtor_route_control').fetchone()
                STATUS['enabled'] = enabled
                if not enabled:
                    STATUS['state']='disabled'
                    return
                if conn.execute("SELECT EXISTS(SELECT 1 FROM jnp_debtor_route_queue WHERE state='uncertain')").fetchone()[0]:
                    pause(conn,'Unresolved write intent: inspect durable audit before resuming')
                    return
                api=AutomaticExact(app_module,conn)
                # Resolve every approved target on every pass, before any write.
                source=await validate_routes(api)
                end=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(seconds=60)
                if end<=cursor:
                    STATUS['state']='waiting'
                    return
                rows=await api.rows('salesentry/SalesEntries',scan_params(source,started,cursor,end))
                enqueue(conn,rows,source,started,end)
                STATUS.update(state='checked',last_scan=m.utcnow())
                queued=conn.execute("SELECT entry_id::text,reference FROM jnp_debtor_route_queue WHERE state='pending' AND next_check<=NOW() ORDER BY next_check,entry_id LIMIT 25").fetchall()
                if not queued:return
                references=sorted({r[1] for r in queued})
                proof=await e.lookup_orders(references)
                for entry_id,reference in queued:
                    order=proof['orders'].get('#'+reference[2:])
                    if order is None:
                        record_state(conn,entry_id,'pending','Order not yet present in Metorik; no inference')
                        continue
                    await process_entry(api,conn,entry_id,reference,order)
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_ID,))


async def serve(app_module):
    while True:
        try:
            await cycle(app_module)
        except asyncio.CancelledError:
            raise
        except Exception:
            # API error bodies and credentials are suppressed. Public health
            # output contains no financial details. Write intents are durable.
            STATUS['state']='error_inspect_private_audit'
        await asyncio.sleep(INTERVAL)


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
