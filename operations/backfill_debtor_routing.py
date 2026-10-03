"""Resumable, explicitly imported Fibonatix catch-up in the existing service.

Only the archived approved cohort is imported. Its stored order evidence is
reused for Customer-only writes, without repeated balance or line checks.
New automatic entries have priority. A daily API reserve is preserved.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

from operations import bacs_debtor_transfer as m, metorik_bacs_evidence as e
from operations import customer_only_routing as customer_only
from operations import routing_runtime as runtime

METHOD = 'wc_fibonatix'
BATCH_SIZE = 10
DAILY_RESERVE = customer_only.DAILY_RESERVE
BATCH_ALLOWANCE = BATCH_SIZE * customer_only.CALLS_PER_ENTRY


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS jnp_debtor_route_backfill (
        entry_id UUID PRIMARY KEY, reference TEXT NOT NULL, order_id BIGINT NOT NULL,
        payment_method TEXT NOT NULL, discovery_sha TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def budget_available(api):
    remaining = api.limits.get('remaining')
    return type(remaining) is int and remaining >= DAILY_RESERVE + BATCH_ALLOWANCE


class Audit:
    def __init__(self, conn, plan):
        self.conn, self.run_id = conn, str(uuid4())
        self.ids = {item['entry_id'] for item in plan['eligible']}
        m.require(bool(self.ids), 'Empty backfill audit')
        self.first = sorted(self.ids)[0]
        self.write_started = False
        self.write_started_entries = set()
        conn.execute('INSERT INTO jnp_debtor_manual_archive(plan_sha256,payment_method,plan,audit,verification) VALUES(%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)',
                     (plan['plan_sha256'], METHOD, json.dumps(plan), '[]', json.dumps({'kind':'automatic_backfill','run_id':self.run_id})))
        self.plan_sha = plan['plan_sha256']

    def persist_event(self, event):
        entry = event.get('entry_id', self.first)
        m.require(entry in self.ids, 'Backfill audit entry outside plan')
        with self.conn.transaction():
            self.conn.execute('UPDATE jnp_debtor_manual_archive SET audit=audit||%s::jsonb WHERE plan_sha256=%s',
                              (json.dumps([event], default=str), self.plan_sha))
            if event['event'] == 'write_intent':
                self.conn.execute("UPDATE jnp_debtor_route_backfill SET state='uncertain',reason='write intent persisted',updated_at=NOW() WHERE entry_id=%s", (entry,))
            elif event['event'] == 'complete':
                completed = {item['entry_id'] for item in event['moved']}
                m.require(completed == self.ids, 'Incomplete backfill result')
                for entry_id in sorted(completed):
                    self.conn.execute("UPDATE jnp_debtor_route_backfill SET state='verified',reason=NULL,updated_at=NOW() WHERE entry_id=%s", (entry_id,))
                self.conn.execute('UPDATE jnp_debtor_manual_archive SET verification=%s::jsonb WHERE plan_sha256=%s',
                                  (json.dumps({'kind':'automatic_backfill','run_id':self.run_id,'result':event},default=str),self.plan_sha))
            elif event['event'] == 'customer_applied':
                self.conn.execute("UPDATE jnp_debtor_route_backfill SET state='applied',reason=NULL,updated_at=NOW() WHERE entry_id=%s", (entry,))
            elif event['event'] == 'entry_failure':
                self.conn.execute("UPDATE jnp_debtor_route_backfill SET state=%s,reason=%s,updated_at=NOW() WHERE entry_id=%s",
                                  (event['state'],event['reason'],entry))
        if event['event'] == 'write_intent':
            self.write_started = True
            self.write_started_entries.add(entry)


async def process_pending(api, conn):
    rows = conn.execute("SELECT entry_id::text,reference,order_id,payment_method FROM jnp_debtor_route_backfill WHERE state='pending' ORDER BY created_at,reference LIMIT %s", (BATCH_SIZE,)).fetchall()
    if not rows: return None
    from operations.automatic_debtor_routing import STATUS
    if not budget_available(api):
        runtime.defer_until_reset(STATUS,api.limits)
        return 'waiting_for_api_budget'
    STATUS['state'] = 'backfill_customer_only'
    m.require(all(row[3] == METHOD for row in rows), 'Unauthorized backfill route')
    accounts = await customer_only.route_accounts(api)
    manifest = [{'entry_id':row[0],'reference':row[1],'order_id':row[2],
                 'payment_method':row[3]} for row in rows]
    p = {'mode':'customer_only','division':m.DIVISION,'created_at':m.utcnow(),
         'run_id':str(uuid4()),'eligible':manifest}
    p['plan_sha256'] = m.digest(p)
    audit = Audit(conn,p)
    for selection in manifest:
        if not customer_only.budget_available(api): break
        try:
            result = await customer_only.change_selected(api,selection,accounts,audit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = customer_only.failure_result(exc, selection['entry_id'] in audit.write_started_entries)
            audit.persist_event({'event':'entry_failure','entry_id':selection['entry_id'],**result})
            runtime.event('entry_isolated',queue='backfill',entry_id=selection['entry_id'],**result)
            STATUS['last_error'] = result['reason']
            if isinstance(exc,m.ExactRequestError) and exc.status_code == 429:
                runtime.defer_until_reset(STATUS,api.limits)
                return 'waiting_for_api_budget'
            if result['stop_cycle']: return 'retry_next_cycle'
            continue
        conn.execute("UPDATE jnp_debtor_route_backfill SET state=%s,reason=%s,updated_at=NOW() WHERE entry_id=%s",
                     (result['state'],result['reason'],selection['entry_id']))
        if result.get('confirmation') == 'Exact HTTP acknowledgement':
            STATUS['applied_since_start'] += 1
    return 'backfill_batch_applied'


async def seed(app, discovery_sha):
    m.require(isinstance(discovery_sha,str) and len(discovery_sha)==64
              and all(c in '0123456789abcdef' for c in discovery_sha), 'Invalid discovery checksum')
    # Live fixed destination/condition must still exist before importing work.
    await m.context(m.Exact(app),METHOD)
    with m.persistent_runner_lock(app), app._db_connect() as conn:
        initialize(conn)
        m.require(conn.execute('SELECT enabled FROM jnp_debtor_route_control').fetchone()==(True,), 'Automatic routing must already be enabled')
        row=conn.execute('SELECT payment_method,plan,verification FROM jnp_debtor_manual_archive WHERE plan_sha256=%s',(discovery_sha,)).fetchone()
        m.require(row is not None and row[0]==METHOD, 'Approved discovery archive missing')
        scope,d=row[1],row[2]
        m.require(scope=={'kind':'read_only_discovery','division':3977752,'source':'100100','destination':'109384'}
                  and m.digest(d)==discovery_sha, 'Discovery scope/checksum mismatch')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(d['checked_at'])).total_seconds()
        m.require(0<=age<=86400, 'Discovery older than 24 hours; refresh before importing')
        manifest=d['manifest']
        m.require(isinstance(manifest,list) and 1<=len(manifest)<=5000,'Invalid discovery size')
        for key in ('entry_id','reference','order_id'):
            m.require(len({r[key] for r in manifest})==len(manifest),'Duplicate discovery mapping')
        for offset in range(0,len(manifest),100): e.manifest_rows(manifest[offset:offset+100])
        with conn.transaction():
            for item in manifest:
                order=d['orders'].get('#'+item['reference'][2:],{})
                m.require(order.get('order_id')==item['order_id'] and order.get('payment_method')==METHOD,'Discovery order evidence mismatch')
                old=conn.execute('SELECT reference,order_id,payment_method FROM jnp_debtor_route_backfill WHERE entry_id=%s',(item['entry_id'],)).fetchone()
                m.require(old is None or old==(item['reference'],item['order_id'],METHOD),'Conflicting existing backfill entry')
                headers=[h for h in d['headers'] if h['EntryID']==item['entry_id']]
                m.require(len(headers)==1,'Discovery header missing or ambiguous')
                differing=m.amount(headers[0]['AmountFC'])!=m.amount(order['total'])
                state='review' if differing else 'pending'
                reason='Exact original amount and order total differ; separate review' if differing else None
                conn.execute('INSERT INTO jnp_debtor_route_backfill(entry_id,reference,order_id,payment_method,discovery_sha,state,reason) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(entry_id) DO NOTHING',
                             (item['entry_id'],item['reference'],item['order_id'],METHOD,discovery_sha,state,reason))
        return {'imported_cohort':len(manifest),'discovery_sha':discovery_sha,'payment_method':METHOD,'destination':'109384'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('seed','status'))
    p.add_argument('--discovery-sha')
    args=p.parse_args()
    from app import main as app
    try:
        if args.action=='seed': result=asyncio.run(seed(app,args.discovery_sha))
        else:
            with app._db_connect() as conn:
                initialize(conn)
                result={'counts':dict(conn.execute('SELECT state,COUNT(*) FROM jnp_debtor_route_backfill GROUP BY state').fetchall()),
                        'review':[{'reference':r[0],'reason':r[1]} for r in conn.execute("SELECT reference,reason FROM jnp_debtor_route_backfill WHERE state IN ('review','uncertain') ORDER BY reference").fetchall()]}
        print(json.dumps(result,default=str))
    except Exception as exc:
        p.exit(2,(str(exc) if isinstance(exc,m.Stop) else 'Backfill stopped; inspect durable audit. Details suppressed.')+'\n')


if __name__=='__main__':main()
