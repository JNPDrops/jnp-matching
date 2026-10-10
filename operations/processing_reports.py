"""Read-only batch report, stored once after every relevant stage is terminal.

Records completed evidence and exceptions; it does not invent live provider
counts, matching results or source completeness that an adapter has not proved.
"""
from datetime import datetime, timezone
import json

from operations import processing_orchestrator as coordinator


def report_data(day,start,end,stages):
    states={stage:state for stage,state,_ in stages}
    expected={stage for stage,_ in coordinator.STAGES if stage!='report'}
    if set(states)!=expected or any(state not in coordinator.TERMINAL for state in states.values()):
        raise ValueError('relevant_day_work_not_terminal')
    status='complete' if all(state=='verified' for state in states.values()) else 'incomplete'
    return {'state':status,'processing_date':day.isoformat(),
        'window_start':start.isoformat(),'window_end':end.isoformat(),
        'generated_at':datetime.now(timezone.utc).isoformat(),
        'workers':{s:{'state':state,'evidence':evidence} for s,state,evidence in stages},
        'financial_writes':False,'matching_policy':'Exact Automatically only'}


def initialize(conn):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('jnp:processing-reports-schema',0))")
        conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_reports (
            batch_key text PRIMARY KEY REFERENCES jnp_processing_batches(batch_key),
            division integer NOT NULL,processing_date date NOT NULL,final boolean NOT NULL,
            state text NOT NULL,data jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now())''')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS jnp_processing_final_report_day
            ON jnp_processing_reports(division,processing_date) WHERE final''')


def create(app,job):
    with app._db_connect() as conn,conn.transaction():
        initialize(conn)
        key=job['task_key']
        batch=conn.execute('''SELECT processing_date,window_start,window_end,final
            FROM jnp_processing_batches WHERE division=3977752 AND batch_key=%s FOR UPDATE''',(key,)).fetchone()
        if not batch:raise ValueError('registered_report_batch_required')
        day,start,end,final=batch
        if conn.execute('''SELECT 1 FROM jnp_processing_stages s
            JOIN jnp_agent_jobs j ON j.job_id=s.job_id
            WHERE s.batch_key=%s AND s.stage<>'report' AND j.state IN ('queued','running') LIMIT 1''',(key,)).fetchone():
            raise ValueError('relevant_worker_still_running')
        stages=conn.execute('''SELECT stage,state,evidence FROM jnp_processing_stages
            WHERE batch_key=%s AND stage<>'report' ORDER BY stage''',(key,)).fetchall()
        data=report_data(day,start,end,stages)
        prior=conn.execute('SELECT state,data FROM jnp_processing_reports WHERE batch_key=%s',(key,)).fetchone()
        if prior:
            return {'state':'verified','report_state':prior[0],'already_reported':True,'financial_writes':False}
        conn.execute('''INSERT INTO jnp_processing_reports(batch_key,division,processing_date,final,state,data)
            VALUES(%s,3977752,%s,%s,%s,%s::jsonb)''',(key,day,final,data['state'],json.dumps(data)))
        return {'state':'verified','report_state':data['state'],'financial_writes':False}
