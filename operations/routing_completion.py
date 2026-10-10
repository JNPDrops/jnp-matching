"""Durable proof of a finished routing cycle, not merely an updated scan cursor."""
from datetime import timedelta
import json

DIVISION = 3977752
MAX_AGE = timedelta(minutes=15)


class RoutingNotReady(ValueError):
    pass


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_routing_completions (
        id bigserial PRIMARY KEY, division integer NOT NULL,
        completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        scanned_through timestamptz NOT NULL, lease_id uuid NOT NULL,
        detail jsonb NOT NULL)''')


def record(conn, owner, detail):
    """Call only at the successful end of the routing cycle while it owns its lock."""
    if owner is None or owner.role != 'routing' or owner.division != DIVISION:
        raise RoutingNotReady('routing_owner_required')
    initialize(conn)
    row = conn.execute('''SELECT c.cursor_at FROM jnp_debtor_route_control c
        JOIN jnp_worker_roles r ON r.division=%s AND r.role='routing'
        WHERE c.enabled AND c.pause_reason IS NULL AND r.lease_id=%s
        AND r.active_owner=%s AND NOT r.draining
        AND r.lease_until>clock_timestamp()''',
        (DIVISION, owner.lease_id, owner.owner)).fetchone()
    if not row or row[0] is None:
        raise RoutingNotReady('routing_completion_owner_or_cursor_invalid')
    conn.execute('''INSERT INTO jnp_routing_completions
        (division,scanned_through,lease_id,detail) VALUES(%s,%s,%s,%s::jsonb)''',
        (DIVISION,row[0],owner.lease_id,json.dumps(detail)))


def validate_proof(proof, now, cutoff=None):
    if not proof:
        raise RoutingNotReady('no_successful_routing_cycle')
    age = now - proof['completed_at']
    if age < timedelta(0) or age > MAX_AGE:
        raise RoutingNotReady('successful_routing_cycle_older_than_15_minutes')
    if cutoff is not None and proof['scanned_through'] < cutoff:
        raise RoutingNotReady('routing_has_not_scanned_batch_cutoff')
    return proof


def require_recent(conn, cutoff=None, references=()):
    # Never manufacture proof in an import worker. Missing migration is a block.
    if not conn.execute("SELECT to_regclass('jnp_routing_completions')").fetchone()[0]:
        raise RoutingNotReady('routing_completion_instrumentation_not_deployed')
    row = conn.execute('''SELECT id,completed_at,scanned_through,clock_timestamp()
        FROM jnp_routing_completions WHERE division=%s
        ORDER BY completed_at DESC,id DESC LIMIT 1''',(DIVISION,)).fetchone()
    if not row:
        raise RoutingNotReady('no_successful_routing_cycle')
    proof=validate_proof({'id':row[0],'completed_at':row[1],
                          'scanned_through':row[2]},row[3],cutoff)
    control=conn.execute('SELECT enabled,pause_reason FROM jnp_debtor_route_control').fetchone()
    if not control or not control[0] or control[1]:
        raise RoutingNotReady('routing_is_paused')
    if references and conn.execute('''SELECT 1 FROM jnp_debtor_route_queue
        WHERE (reference=ANY(%s) OR order_reference=ANY(%s))
        AND state IN ('pending','uncertain') LIMIT 1''',
        (list(references),list(references))).fetchone():
        raise RoutingNotReady('selected_orders_have_unfinished_routing')
    return {k:v.isoformat() if hasattr(v,'isoformat') else v for k,v in proof.items()}


def check_app(app, cutoff=None, references=()):
    if app.DIVISION != DIVISION:
        raise RoutingNotReady('wrong_administration')
    with app._db_connect() as conn:
        return require_recent(conn,cutoff,references)
