"""One confirmed tax rule write per owned operation; no bank-entry writes."""
from operations import worker_coordination as coordination
from operations import worker_write_fence as fence


class CreationDeferred(Exception):
    """Budget refused before any POST was admitted."""


class ConfirmationRequired(Exception):
    pass


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_tax_rule_attempts (
        operation_id UUID PRIMARY KEY, subject TEXT NOT NULL, action TEXT NOT NULL,
        state TEXT NOT NULL, rule_id UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


def audit(conn, subject, action, state, rule_id=None):
    operation = fence.audit_metadata().get('worker_operation_id')
    if operation:
        conn.execute('''INSERT INTO jnp_tax_rule_attempts
            (operation_id,subject,action,state,rule_id) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(operation_id) DO UPDATE SET state=EXCLUDED.state,
            rule_id=EXCLUDED.rule_id,updated_at=NOW()''',
            (operation, subject, action, state, rule_id))


@fence.owned_operation('tax')
async def create_confirmed_rule(conn, api, payload):
    from operations.tax_allocation import ROOT, match_rule, save_state
    words = payload['Words']
    save_state(conn, words, 'creating')
    audit(conn, words, 'POST', 'intent')
    try:
        try:
            await api.request('POST', ROOT, payload=payload)
        except coordination.BudgetDeferred:
            # The shared budget is checked before transport/write admission.
            save_state(conn, words, 'pending', reason='API budget deferred before POST')
            audit(conn, words, 'POST', 'deferred')
            raise CreationDeferred() from None
        rules = await api.rules()
        state, rule_id = match_rule(rules, payload)
        if state != 'confirmed':
            raise ConfirmationRequired('tax_rule_readback_not_unique')
        save_state(conn, words, 'confirmed', rule_id)
        audit(conn, words, 'POST', 'confirmed', rule_id)
        return rules
    except CreationDeferred:
        raise
    except BaseException:
        save_state(conn, words, 'uncertain', reason='Creation requires confirmation')
        audit(conn, words, 'POST', 'uncertain')
        raise


@fence.owned_operation('tax')
async def retire_confirmed_rule(conn, api, rule, tax_account_id):
    from operations.tax_allocation import legacy_creditor_fallback
    from operations.allocation_maintenance import delete_rule
    if not legacy_creditor_fallback(rule, tax_account_id):
        raise ConfirmationRequired('not_the_verified_legacy_tax_rule')
    subject = rule['ID']
    audit(conn, subject, 'DELETE', 'intent', subject)
    try:
        state = await delete_rule(conn, api, rule, 'tax_iban_creditor_fallback')
        if state == 'uncertain':
            raise ConfirmationRequired('tax_rule_deletion_unconfirmed')
        audit(conn, subject, 'DELETE', state, subject)
        return state
    except BaseException:
        audit(conn, subject, 'DELETE', 'uncertain', subject)
        raise
