"""Audited single-write adapters for existing allocation maintenance scope."""
from operations import worker_write_fence as fence
from operations.worker_coordination import BudgetDeferred


class RuleUnconfirmed(Exception):
    pass


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_maintenance_rule_attempts (
        operation_id UUID PRIMARY KEY, subject TEXT NOT NULL, action TEXT NOT NULL,
        state TEXT NOT NULL, rule_id UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


def audit(conn, subject, action, state, rule_id=None):
    operation = fence.audit_metadata().get('worker_operation_id')
    if operation:
        conn.execute('''INSERT INTO jnp_maintenance_rule_attempts
            (operation_id,subject,action,state,rule_id) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(operation_id) DO UPDATE SET state=EXCLUDED.state,
            rule_id=EXCLUDED.rule_id,updated_at=NOW()''',
            (operation, subject, action, state, rule_id))


def save(conn, words, state, rule_id=None):
    conn.execute('UPDATE jnp_suspense_rules SET state=%s,rule_id=%s WHERE words=%s',
                 (state, rule_id, words))


@fence.owned_operation('maintenance')
async def create_confirmed_rule(conn, api, payload):
    from operations.allocation_maintenance import ROOT, signature
    words = payload['Words']
    save(conn, words, 'creating')
    audit(conn, words, 'POST', 'intent')
    api.allowed_posts[words] = payload
    try:
        try:
            await api.request('POST', ROOT, payload=payload)
        except BudgetDeferred:
            save(conn, words, 'pending')
            audit(conn, words, 'POST', 'deferred')
            return 'pending'
        # Reject other rules on the same recognition words, not just duplicates
        # of the desired payload: an operator may have edited it during POST.
        found = [r for r in await api.rules() if str(r.get('Words') or '').strip() == words]
        if len(found) != 1 or signature(found[0]) != signature(payload):
            raise RuleUnconfirmed('maintenance_rule_readback_not_unique')
        rule_id = found[0]['ID']
        save(conn, words, 'confirmed', rule_id)
        audit(conn, words, 'POST', 'confirmed', rule_id)
        return 'confirmed'
    except BaseException:
        save(conn, words, 'uncertain')
        audit(conn, words, 'POST', 'uncertain')
        raise
    finally:
        api.allowed_posts.pop(words, None)


@fence.owned_operation('maintenance')
async def delete_confirmed_rule(conn, api, rule, reason, keeper=None, checkpoint=None):
    from operations.allocation_maintenance import delete_rule
    subject = rule['ID']
    audit(conn, subject, 'DELETE', 'intent', subject)
    try:
        state = await delete_rule(conn, api, rule, reason, keeper)
        if state == 'uncertain':
            raise RuleUnconfirmed('maintenance_deletion_unconfirmed')
        if state == 'deleted' and checkpoint:
            checkpoint()
        audit(conn, subject, 'DELETE', state, subject)
        return state
    except BaseException:
        audit(conn, subject, 'DELETE', 'uncertain', subject)
        raise
