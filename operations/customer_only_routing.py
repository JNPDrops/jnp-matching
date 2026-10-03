"""Approved debtor-only executor for the automatic queue and backfill.

Use existing order evidence. Read only entry identity and its current open item,
then send one Customer-only PUT. No balance, line, VAT or post-write rereads.
"""
import re

from operations import bacs_debtor_transfer as m

DAILY_RESERVE = 100
CALLS_PER_ENTRY = 3
_accounts = None


def failure_result(exc, write_started):
    """Keep an unresolved write isolated; never repeat an ambiguous outcome."""
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


async def route_accounts(api):
    """Resolve existing debtor IDs once per service process, never create them."""
    global _accounts
    if _accounts is None:
        codes = {m.SOURCE, *(route[0] for route in m.ROUTES.values())}
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


async def change_selected(api, selection, accounts, audit):
    """Apply one previously established order-to-entry mapping without replanning."""
    method, reference = selection['payment_method'], selection['reference']
    m.require(method in m.ROUTES or method in m.RETAIN_ON_SOURCE, 'Unauthorized payment route')
    entry_id = m.guid(selection['entry_id'])
    m.require(isinstance(reference, str) and re.fullmatch(r'TD[0-9]{4,10}', reference),
              'Invalid approved order reference')
    m.require(type(selection['order_id']) is int and selection['order_id'] > 0,
              'Missing approved order identity')
    if method in m.RETAIN_ON_SOURCE:
        # A policy disposition only: no Exact reread, write, or reversal of an
        # earlier transfer. Applies to old queued work as well as new imports.
        return {**selection, 'entry_id': entry_id, 'destination': m.RETAIN_ON_SOURCE[method],
                'state': 'retained', 'reason': 'Fibonatix transfer disabled; retain existing debtor'}
    source = m.guid(accounts[m.SOURCE])
    destination = m.guid(accounts[m.ROUTES[method][0]])
    m.require(source != destination, 'Source and destination must differ')
    result = {**selection, 'entry_id': entry_id, 'destination': m.ROUTES[method][0]}
    if not budget_available(api):
        return {**result, 'state': 'pending', 'reason': 'Waiting for API budget'}

    rows = await api.rows('salesentry/SalesEntries', {
        '$filter': f"EntryID eq guid'{entry_id}'",
        '$select': 'EntryID,Customer,YourRef,EntryNumber,Status,Type,Reversal'})
    if len(rows) != 1:
        return {**result, 'state': 'review', 'reason': 'Sales entry missing or ambiguous'}
    header = rows[0]
    if header['EntryID'] != entry_id or header['YourRef'] != reference:
        return {**result, 'state': 'review', 'reason': 'Entry/order mapping changed'}
    if header['Customer'] == destination:
        return {**result, 'state': 'applied', 'reason': 'Already on destination debtor'}
    if header['Customer'] != source:
        return {**result, 'state': 'review', 'reason': 'Entry is no longer on source debtor'}
    if header['Status'] != 20 or header['Type'] != 20 or header['Reversal'] is not False:
        return {**result, 'state': 'review', 'reason': 'Entry is not an editable sales booking'}

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
    if m.amount(item['Amount']) <= 0:
        return {**result, 'state': 'skipped', 'reason': 'No positive remaining amount'}
    if not budget_available(api, calls=1):
        return {**result, 'state': 'pending', 'reason': 'Waiting for API budget'}

    m.append_audit(audit, {'event': 'write_intent', 'entry_id': entry_id,
                         'mode': 'customer_only', 'selection': result,
                         'before_customer': source, 'payload': {'Customer': destination}})
    # Never retry an ambiguous write. The caller pauses with the durable intent.
    await api.change_customer(entry_id, destination)
    result.update(state='applied', reason=None, confirmation='Exact HTTP acknowledgement')
    m.append_audit(audit, {'event': 'customer_applied', **result})
    return result
