"""Completed day-boundary evidence from the existing continuous rule workers.

Observers cannot create a completion on a worker's behalf. The writer must hold
the current role lease, and the role calls this only after its actual work.
"""
from datetime import datetime, timezone
import json

ROLES={'woo-rules','tax','maintenance'}


def initialize(conn):
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('jnp:processing-completions-schema',0))")
        conn.execute('''CREATE TABLE IF NOT EXISTS jnp_processing_completions (
            id bigserial PRIMARY KEY,division integer NOT NULL,role text NOT NULL,
            completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            scanned_through timestamptz NOT NULL,lease_id uuid NOT NULL,
            detail jsonb NOT NULL)''')


def record(conn,role,scanned_through,detail):
    from operations.worker_write_fence import current_owner
    owner=current_owner()
    if role not in ROLES or owner is None or owner.role!=role or owner.division!=3977752:
        raise ValueError('completed_role_owner_required')
    if scanned_through.tzinfo is None or scanned_through>datetime.now(timezone.utc):
        raise ValueError('invalid_completion_watermark')
    initialize(conn)
    if not conn.execute('''SELECT 1 FROM jnp_worker_roles WHERE division=3977752
        AND role=%s AND lease_id=%s AND active_owner=%s AND NOT draining
        AND lease_until>clock_timestamp()''',(role,owner.lease_id,owner.owner)).fetchone():
        raise ValueError('completed_role_lease_unavailable')
    conn.execute('''INSERT INTO jnp_processing_completions
        (division,role,scanned_through,lease_id,detail) VALUES(3977752,%s,%s,%s,%s::jsonb)''',
        (role,scanned_through,owner.lease_id,json.dumps(detail)))


def proof(conn,role,cutoff):
    if role not in ROLES:
        raise ValueError('unknown_completion_role')
    if not conn.execute("SELECT to_regclass('jnp_processing_completions')").fetchone()[0]:
        return None
    row=conn.execute('''SELECT id,completed_at,scanned_through,detail
        FROM jnp_processing_completions WHERE division=3977752 AND role=%s
        AND scanned_through>=%s ORDER BY completed_at DESC,id DESC LIMIT 1''',(role,cutoff)).fetchone()
    if not row:
        return None
    if role=='woo-rules' and conn.execute('''SELECT 1 FROM jnp_woo_iban_events
        WHERE created_at<=%s AND state IN ('pending','creating','uncertain') LIMIT 1''',(cutoff,)).fetchone():
        return None
    return {'id':row[0],'completed_at':row[1].isoformat(),
            'scanned_through':row[2].isoformat(),'detail':row[3]}
