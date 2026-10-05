"""Approved debtor-only executor for the automatic queue and backfill.

Use existing order evidence. Read only entry identity and its current open item,
then send one Customer-only PUT. No balance, line, VAT or post-write rereads.
"""
import re

from operations import bacs_debtor_transfer as m
from operations import debtor_routing_policy as policy
from operations.worker_coordination import BudgetDeferred as SharedBudgetDeferred
from operations.worker_write_fence import audit_metadata, owned_operation

DAILY_RESERVE = 100
CALLS_PER_ENTRY = 3
_accounts = None


class BudgetDeferred(m.Stop):
    """Wait for the Exact reset without consuming the daily reserve."""


def destination_code(method):
    m.require(method in policy.CLEANUP_ROUTES, "Unauthorized payment route")
    return policy.CLEANUP_ROUTES[method]


def failure_result(exc, write_started):
    """Keep an unresolved write isolated; never repeat an ambiguous outcome."""
    if isinstance(exc, (BudgetDeferred, SharedBudgetDeferred)):
        return {'state': 'pending', 'reason': 'Waiting for API budget', 'stop_cycle': True}
    if isinstance(exc, m.WritePaused):
        return {'state': 'pending', 'reason': 'Operator paused before PUT', 'stop_cycle': True}
    if isinstance(exc, m.Stop) and str(exc).startswith(('Exact GET transport/auth failure', 'Exact PUT transport/auth failure')):
        return {'state': 'uncertain' if write_started else 'pending',
                'reason': 'Exact transport/auth failure', 'stop_cycle': True}
    if isinstance(exc, m.ExactRequestError):
        reason = f'Exact {exc.method} HTTP {exc.status_code}'
        if exc.status_code in (401, 403, 429):
            # Explicit rejection, not an ambiguous network/5xx outcome.
            return {'state': 'pending', 'reason': reason, 'stop_cycle': True}
        if 400 <= exc.status_code < 500:
            return {'state': 'review', 'reason': reason, 'stop_cycle': False}
        return {'state': 'uncertain' if write_started else 'pending',
                'reason': reason, 'stop_cycle': True}
    return {'state': 'uncertain' if write_started else 'review',
            'reason': 'Unconfirmed write outcome' if write_started else 'Entry validation/read failed',
            'stop_cycle': False}


async def route_accounts(api, methods=None):
    """Resolve existing debtor IDs once per service process, never create them."""
    global _accounts
    codes = {m.SOURCE, *(destination_code(method) for method in (m.ROUTES if methods is None else methods))}
    if _accounts is None or not codes <= set(_accounts):
        rows = await api.rows('crm/Accounts', {
            '$filter': ' or '.join('Code eq ' + m.quoted(code.rjust(18)) for code in sorted(codes)),
            '$select': 'ID,Code,IsSales,Status'})
        found = {}
        for code in codes:
            selected = [r for r in rows if r['Code'].strip() == code]
            m.require(len(selected) == 1 and selected[0]['IsSales'] is True
                      and selected[0]['Status'] == 'C', 'Existing route debtor missing or ambiguous')
            found[code] = m.guid(selected[0]['ID'])
        m.require(len(set(found.values())) == len(codes), 'Route debtors must differ')
        _accounts = found
    return dict(_accounts)


def budget_available(api, calls=CALLS_PER_ENTRY):
    remaining = api.limits.get('remaining')
    return type(remaining) is int and remaining >= DAILY_RESERVE + calls


@owned_operation('routing')
async def change_selected(api, selection, accounts, audit):
    """Apply one previously established order-to-entry mapping without replanning."""
    method, reference = selection['payment_method'], selection['reference']
    scope = selection.get('work_scope', 'continuous')
    m.require(method in policy.routes(scope) or method in policy.RETAIN_ON_SOURCE, 'Unauthorized payment route')
    entry_id = m.guid(selection['entry_id'])
    m.require(isinstance(reference, str) and re.fullmatch(r'TD[0-9]{4,10}', reference),
              'Invalid approved order reference')
    m.require(type(selection['order_id']) is int and selection['order_id'] > 0,
              'Missing approved order identity')
    entry_type = selection.get('entry_type', 20)
    order_reference = selection.get('order_reference', reference)
    m.require(entry_type in (20, 21), 'Unsupported sales entry type')
    if entry_type == 21:
        m.require(scope == 'cleanup' and isinstance(order_reference, str)
                  and re.fullmatch(r'TD[0-9]{4,10}', order_reference)
                  and order_reference != reference, 'Credit needs its original order')
        m.require(m.guid(selection.get('debit_entry_id')) != entry_id,
                  'Credit needs its proven original debit entry')
    else:
        m.require(order_reference == reference, 'Order reference mismatch')
    if method in policy.RETAIN_ON_SOURCE:
        # A policy disposition only: no Exact reread, write, or reversal of an
        # earlier transfer. Applies to old queued work as well as new imports.
        return {**selection, 'entry_id': entry_id, 'destination': policy.RETAIN_ON_SOURCE[method],
                'state': 'retained', 'reason': 'Fibonatix transfer disabled; retain existing debtor'}
    source = m.guid(accounts[m.SOURCE])
    destination = m.guid(accounts[destination_code(method)])
    m.require(source != destination, 'Source and destination must differ')
    result = {**selection, 'entry_id': entry_id, 'destination': destination_code(method)}
    if not budget_available(api):
        return {**result, 'state': 'pending', 'reason': 'Waiting for API budget'}

    rows = await api.rows('salesentry/SalesEntries', {
        '$filter': f"EntryID eq guid'{entry_id}'",
        '$select': 'EntryID,Customer,YourRef,EntryNumber,Status,Type,Reversal,Description'})
    if len(rows) != 1:
        return {**result, 'state': 'review', 'reason': 'Sales entry missing or ambiguous'}
    header = rows[0]
    if header['EntryID'] != entry_id or header['YourRef'] != reference:
        return {**result, 'state': 'review', 'reason': 'Entry/order mapping changed'}
    if header['Customer'] == destination:
        return {**result, 'state': 'applied', 'reason': 'Already on destination debtor'}
    if header['Customer'] != source:
        return {**result, 'state': 'review', 'reason': 'Entry is no longer on source debtor'}
    if header['Status'] != 20 or header['Type'] != entry_type or header['Reversal'] is not False:
        return {**result, 'state': 'review', 'reason': 'Entry is not an editable sales booking'}
    if entry_type == 21 and header.get('Description') != f'Order #{order_reference[2:]} / Credit #{reference}':
        return {**result, 'state': 'review', 'reason': 'Credit/original order mapping changed'}

    # This selects the currently open item only; it is not a balance comparison.
    open_items = await api.rows('read/financial/ReceivablesList', {
        '$filter': f"EntryNumber eq {int(header['EntryNumber'])} and YourRef eq {m.quoted(reference)} and AccountId eq guid'{source}'",
        '$select': 'AccountId,EntryNumber,YourRef,Amount'})
    if not open_items:
        return {**result, 'state': 'skipped', 'reason': 'No remaining open item'}
    if len(open_items) != 1:
        return {**result, 'state': 'review', 'reason': 'Ambiguous open item'}
    item = open_items[0]
    m.require(item['AccountId'] == source and item['YourRef'] == reference
              and item['EntryNumber'] == header['EntryNumber'], 'Unexpected open item')
    remaining = m.amount(item['Amount'])
    if (entry_type == 20 and remaining <= 0) or (entry_type == 21 and remaining >= 0):
        return {**result, 'state': 'skipped', 'reason': 'No remaining amount for this entry type'}
    if not budget_available(api, calls=1):
        return {**result, 'state': 'pending', 'reason': 'Waiting for API budget'}

    m.append_audit(audit, {'event': 'write_intent', 'entry_id': entry_id,
                         'mode': 'customer_only', 'selection': result,
                         'before_customer': source, 'payload': {'Customer': destination},
                         **audit_metadata()})
    # Never retry an ambiguous write. The caller pauses with the durable intent.
    await api.change_customer(entry_id, destination)
    result.update(state='applied', reason=None, confirmation='Exact HTTP acknowledgement')
    m.append_audit(audit, {'event': 'customer_applied', **result, **audit_metadata()})
    return result
