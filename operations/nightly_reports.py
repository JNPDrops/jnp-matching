"""One durable read-only report for a terminal calendar-day batch."""
import json
from datetime import date, datetime, timezone
from operations import nightly_batches as n
from operations.worker_write_fence import current_owner


async def run(app, job):
    if job['action'] != 'daily_report' or set(job['params']) != {'date'}:
        raise ValueError('invalid_daily_report')
    day = date.fromisoformat(job['params']['date'])
    if job['task_key'] != f'jnp:{n.DIVISION}:report:{day.isoformat()}' or app.DIVISION != n.DIVISION:
        raise ValueError('wrong_report_scope')
    owner = current_owner()
    if owner is None or owner.role != 'reports':
        raise ValueError('assigned_reports_worker_required')
    with app._db_connect() as conn, conn.transaction():
        conn.execute('''CREATE TABLE IF NOT EXISTS jnp_daily_reports (
            division integer NOT NULL,processing_date date NOT NULL,
            state text NOT NULL,data jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(division,processing_date))''')
        batch = conn.execute('''SELECT window_start,window_end FROM jnp_nightly_runs
            WHERE division=%s AND processing_date=%s FOR UPDATE''',(n.DIVISION,day)).fetchone()
        if not batch:
            raise ValueError('day_not_registered')
        stages = conn.execute('''SELECT stage,state,evidence FROM jnp_nightly_stages
            WHERE division=%s AND processing_date=%s''',(n.DIVISION,day)).fetchall()
        outcome = n.report_gate({stage:state for stage,state,_ in stages})
        prior = conn.execute('SELECT state FROM jnp_daily_reports WHERE division=%s AND processing_date=%s',(n.DIVISION,day)).fetchone()
        if prior:
            return {'state':prior[0],'already_reported':True,'financial_writes':False}
        from operations.fibonatix_daily_review import initialize
        initialize(conn)
        coverage=conn.execute('''SELECT state,data FROM jnp_fibonatix_daily_reviews
            WHERE division=%s AND processing_date=%s ORDER BY review_date DESC LIMIT 1''',(n.DIVISION,day)).fetchone()
        if conn.execute('''SELECT 1 FROM jnp_agent_jobs WHERE division=%s AND task_key=%s
            AND state IN ('queued','running','uncertain') LIMIT 1''',(n.DIVISION,n.identity(day,'fibonatix'))).fetchone():
            raise ValueError('fibonatix_day_work_not_terminal')
        if coverage is None or coverage[0]!='complete':
            outcome='incomplete'
        report = {'processing_date':day.isoformat(),'state':outcome,
                  'fibonatix_coverage':coverage[1] if coverage else {'state':'missing','reason':'daily_receipt_audit_missing'},
                  'window_start':batch[0].isoformat(),'window_end':batch[1].isoformat(),
                  'generated_at':datetime.now(timezone.utc).isoformat(),
                  'workers':{stage:{'state':state,'evidence':evidence} for stage,state,evidence in stages if stage!='reports'},
                  'financial_writes':False,'matching_policy':'Exact Automatically only'}
        conn.execute('INSERT INTO jnp_daily_reports(division,processing_date,state,data) VALUES(%s,%s,%s,%s::jsonb)',(n.DIVISION,day,outcome,json.dumps(report)))
        n.record(conn,day,'reports','completed',{'verified':True,'report_state':outcome,'report_key':job['task_key'],'financial_writes':False})
        conn.execute('UPDATE jnp_nightly_runs SET state=%s,detail=detail||%s::jsonb,updated_at=now() WHERE division=%s AND processing_date=%s',
                     (outcome,json.dumps({'report_key':job['task_key']}),n.DIVISION,day))
    return {'state':outcome,'financial_writes':False}
