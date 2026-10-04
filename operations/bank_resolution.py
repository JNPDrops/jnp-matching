"""Evidence and next actions for open bank payments; never posts or writes off.

Allocation and matching are different states. An open cashflow record proves an
unmatched balance; a bank line on 1100/1400 by itself does not. All identities and
currency come from Exact, not from the merchant description.
"""
from collections import defaultdict
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from operations import bacs_debtor_transfer as m, tax_reference as tax

VERSION = 'bank-resolution-v1'
REFUND = re.compile(r'refund|double payment|duplicate payment|overpayment|terugbetaling|teruggestort|cancell?ation|cancelled|canceled|annulering', re.I)
PROVIDER = re.compile(r'icepay|paynetics|fibon[a-z]*|stripe|plisio|ninja|suap|myco', re.I)
SOURCE = re.compile(r'(?<![a-z0-9_])(?:bosci|decao)_[a-f0-9]{29}(?:[a-f0-9]{3})?(?![a-z0-9_])', re.I)
OPEN_FIELDS = 'HID,AccountId,AccountCode,AccountName,Amount,CurrencyCode,InvoiceDate,YourRef,EntryNumber,JournalCode,Description'
CASH_FIELDS = 'ID,Account,AccountCode,AccountName,AmountDC,AmountFC,Currency,Description,InvoiceDate,TransactionID,TransactionEntryID,EntryNumber,Journal,Status,GLAccountCode'


def code(value):
    return str(value or '').strip()


def is_psp(bank):
    journal = ' '.join(code(bank.get(k)) for k in ('JournalDescription', 'BankAccountDescription'))
    if bank.get('JournalType') == 16 or PROVIDER.search(journal) or re.search('paypal', journal, re.I):
        return True
    desc = code(bank.get('Description'))
    # A PAYPAL*merchant card purchase is not a PSP settlement.
    return bool(PROVIDER.search(desc) or (m.amount(bank.get('AmountDC')) > 0
        and re.search(r'paypal europe|paypal.*(?:payout|settlement|uitbetaling)', desc, re.I)))


def reference(description):
    explicit = re.findall(r'(?<![A-Za-z0-9])(?:TD|order(?:\s*(?:number|nummer|no\.?))?\s*[:#-]?\s*)([0-9]{4,7})(?![0-9])', description, re.I)
    numbers = explicit or re.findall(r'(?<![A-Za-z0-9])([0-9]{5,6})(?![A-Za-z0-9])', SOURCE.sub('', description))
    return 'TD' + numbers[0] if len(set(numbers)) == 1 else None


def enrich_bank(bank, headers, journals):
    b = dict(bank)
    header = headers.get(code(b.get('EntryID')).lower(), {})
    journal = journals.get(code(header.get('JournalCode')), {})
    b.update(Currency=header.get('Currency'), JournalCode=header.get('JournalCode'),
        JournalDescription=journal.get('Description') or header.get('JournalDescription'),
        JournalType=journal.get('Type'), BankAccountDescription=journal.get('BankAccountDescription'))
    return b


async def load_headers(api, entry_ids):
    """Read parents of current candidates, never the complete bank history."""
    result = {}
    entries = sorted({m.guid(e) for e in entry_ids})
    for start in range(0, len(entries), 15):
        selection = ' or '.join("EntryID eq guid'" + e + "'" for e in entries[start:start + 15])
        rows = await api.rows('financialtransaction/BankEntries', params={'$filter': selection,
            '$select': 'EntryID,JournalCode,JournalDescription,Currency'})
        wanted = set(entries[start:start + 15])
        for h in rows:
            if code(h.get('EntryID')).lower() not in wanted:
                raise m.Stop('Unrequested bank header returned')
            key = code(h['EntryID']).lower()
            if key in result:
                raise m.Stop('Duplicate bank header identity')
            result[key] = h
    return result


def duplicate_candidates(banks):
    """Flag repeated identities; never infer deletion from date+amount alone."""
    groups = defaultdict(list)
    for b in banks:
        if not b.get('JournalCode') or not b.get('Currency'):
            continue
        desc = code(b.get('Description')).lower()
        ids = SOURCE.findall(desc)
        # Normalize source suffixes so a short and full CAMT description compare.
        name = SOURCE.split(desc)[0].strip() if ids else desc
        name = ' '.join(re.findall(r'[a-z0-9]+', name))
        base = (code(b['JournalCode']), str(tax.bank_date(b['Date'])), b['Currency'], str(m.amount(b['AmountFC'])))
        if len(name) >= 6:
            groups[base + ('name', name)].append(b)
        for src in ids:
            groups[base + ('source', src[:35])].append(b)
    peers = defaultdict(set)
    for group in groups.values():
        unique = {code(b['ID']).lower() for b in group}
        if len(unique) > 1:
            for key in unique:
                peers[key].update(unique - {key})
    return {k: sorted(v) for k, v in peers.items()}


def invoice_rows(rows, journals, purchase=False):
    types = (22, 23) if purchase else (20, 21)
    return [r for r in rows if journals.get(code(r.get('JournalCode')), {}).get('Type') in types]


def amount_context(item, bank, invoice):
    original = abs(m.amount(bank['AmountDC']))
    remaining = abs(m.amount(bank.get('OpenAmountDC', bank['AmountDC'])))
    due = abs(m.amount(invoice['Amount']))
    item.update(original_bank_amount=f'{original:.2f}', remaining_bank_amount=f'{remaining:.2f}',
        invoice_open_amount=f'{due:.2f}', difference=f'{remaining - due:.2f}',
        invoice_entry=invoice.get('EntryNumber'), invoice_hid=invoice.get('HID'),
        invoice_reference=invoice.get('YourRef'))
    return remaining == due


def finish(item, status, action, reason, retry='na nieuwe factuur, betaling of correctie'):
    item.update(status=status, next_action=action, reason=reason, retry_when=retry,
        match_executed=False, bank_write=False)
    return item


def review(bank, old, receivables, payables, journals, peers):
    item = dict(old)
    item.update(gl_account=code(bank.get('GLAccountCode')), account_code=code(bank.get('AccountCode')),
        journal_code=code(bank.get('JournalCode')), journal=code(bank.get('JournalDescription')),
        currency=bank.get('Currency'), entry_number=bank.get('EntryNumber'),
        review_version=VERSION, open_evidence=bank.get('OpenEvidence', 'suspense_1360'))
    if tax.tax_hint(bank):
        return finish(item, old['status'], 'Belastingsoort en balansrekening controleren; belastingregel toepassen', old['reason'])
    if is_psp(bank):
        return finish(item, 'psp_deferred', 'Afhandelen via het eigen PSP-bankboek en de afgesproken tussenrekening',
            'PSP-bankboek of PSP-uitbetaling herkend; buiten gewone bankmatching')
    if peers.get(code(bank['ID']).lower()):
        item['possible_duplicate_ids'] = peers[code(bank['ID']).lower()]
        return finish(item, 'duplicate_review', 'Vergelijk oorspronkelijke banktransactie en import; pas daarna afletteren',
            'Zelfde bronkenmerk of tegenpartij, eigen bankboek, datum, valuta en bedrag; nog geen bewijs van duplicaat')
    if bank.get('Currency') != 'EUR' or m.amount(bank.get('AmountFC')) != m.amount(bank['AmountDC']):
        return finish(item, 'currency_review', 'Controleer oorspronkelijke valuta en koers', 'EUR-bankbedrag niet bevestigd')
    desc, amount = code(bank.get('Description')), m.amount(bank['AmountDC'])
    gl, account = item['gl_account'], code(bank.get('Account')).lower()
    if REFUND.search(desc) and amount < 0:
        item['reference'] = reference(desc)
        return finish(item, 'refund_review', 'Koppel terugbetaling aan oorspronkelijke ontvangst of bijbehorende creditnota',
            'Klantterugbetaling/annulering; geen gewone leverancierskosten')
    if gl == '1400':
        same = [r for r in invoice_rows(payables, journals, True) if code(r.get('AccountId')).lower() == account]
        normalized = re.sub(r'[^A-Z0-9]', '', desc.upper())
        by_ref = [r for r in same if len(re.sub(r'[^A-Z0-9]', '', code(r.get('YourRef')).upper())) >= 5
            and re.sub(r'[^A-Z0-9]', '', code(r.get('YourRef')).upper()) in normalized]
        if len(by_ref) > 1:
            return finish(item, 'multiple_invoices', 'Controleer welke factuur of factuurcombinatie is betaald', 'Meerdere factuurreferenties passen')
        candidates = by_ref or [r for r in same if r.get('CurrencyCode') == 'EUR' and m.amount(r['Amount']) == -amount]
        if len(candidates) != 1:
            return finish(item, 'supplier_invoice_missing' if not same else 'supplier_invoice_review',
                'Zoek of importeer de inkoopfactuur; controleer ook eerdere aflettering en de gekozen leverancier',
                'Geen unieke openstaande inkoopfactuur of creditnota gevonden')
        inv = candidates[0]
        equal = amount_context(item, bank, inv)
        if inv.get('CurrencyCode') != 'EUR' or m.amount(inv['Amount']) * amount >= 0:
            return finish(item, 'invoice_direction_review', 'Controleer valuta en factuur versus creditnota', 'Factuur en bankbetaling hebben geen tegengestelde richting')
        if not equal:
            return finish(item, 'amount_review', 'Vergelijk factuur, betaling en eerdere deelafletteringen', 'Openstaand factuurbedrag wijkt af; verschil niet afboeken')
        if not by_ref:
            return finish(item, 'supplier_amount_candidate', 'Bevestig factuurreferentie op betalingsbewijs', 'Leverancier en bedrag passen; referentie ontbreekt')
        return finish(item, 'supplier_match_candidate', 'Letter deze betaling af tegen de genoemde factuur na actuele identiteitscontrole',
            'Leverancier, factuurreferentie, valuta en openstaand bedrag passen exact')
    if amount > 0:
        ref = reference(desc)
        item['reference'] = ref
        if not ref:
            return finish(item, 'receipt_identity_needed', 'Zoek order of herkomst via betaalbewijs, naam en bedrag', 'Geen unieke orderreferentie in ontvangst')
        matches = [r for r in invoice_rows(receivables, journals) if code(r.get('YourRef')).upper() == ref]
        if len(matches) != 1:
            return finish(item, 'invoice_missing' if not matches else 'multiple_invoices',
                'Controleer orderstatus, factuurimport en eerdere aflettering; probeer opnieuw zodra de factuur beschikbaar is',
                'Geen unieke openstaande verkoopfactuur')
        inv = matches[0]
        equal = amount_context(item, bank, inv)
        if inv.get('CurrencyCode') != 'EUR' or m.amount(inv['Amount']) <= 0:
            return finish(item, 'invoice_direction_review', 'Controleer valuta en factuur versus creditnota', 'Geen positieve EUR-verkoopfactuur')
        if code(inv.get('AccountCode')) != '109372' or (gl == '1100' and account != code(inv.get('AccountId')).lower()):
            return finish(item, 'account_review', 'Controleer betaalmethode en debiteur van order, factuur en bankregel', 'Factuur en banktransferdebiteur komen niet overeen')
        item['receivable'] = inv
        if not equal:
            return finish(item, 'amount_review', 'Vergelijk webshopordertotaal, factuur en eerdere deelafletteringen', 'Openstaand factuurbedrag wijkt af; verschil niet afboeken')
        return finish(item, 'order_evidence_needed', 'Verifieer BACS-betaalmethode, orderbedrag en besteldatum',
            'Unieke orderreferentie en gelijk openstaand bedrag; orderbewijs nog nodig')
    if gl == '1100':
        return finish(item, 'refund_review', 'Zoek oorspronkelijke klantontvangst of creditnota', 'Uitgaande betaling op debiteuren')
    return finish(item, old['status'], 'Controleer leverancier en inkoopfactuur of bon; pas daarna toewijzen', old['reason'])


def order_evidence(item, order):
    if not item.get('reference') or item['status'] in ('psp_deferred', 'duplicate_review', 'refund_review'):
        return item
    if not order or order.get('order_number') != '#' + item['reference'][2:]:
        item['order_check'] = 'order_not_found'
        return item
    item['order_check'] = 'observed'
    item['order_total'] = str(order.get('total'))
    item['order_status'] = order.get('status')
    item['payment_method'] = order.get('payment_method')
    if order.get('payment_method') != 'bacs':
        return finish(item, 'payment_method_review', 'Controleer betaalstroom en debiteur; geen BACS-regel aanmaken', 'Webshoporder is niet BACS')
    if order.get('currency') != 'EUR' or order.get('total_refunds') is None or m.amount(order['total_refunds']) != 0:
        return finish(item, 'order_refund_review', 'Controleer terugbetalingen en valuta van de order', 'Order heeft een terugbetaling of afwijkende valuta')
    try:
        created = datetime.fromisoformat(str(order.get('order_created_at') or '').replace('Z', '+00:00'))
        if created.tzinfo is None:
            raise ValueError('timezone missing')
        if tax.bank_date(item['bank_date']) < created.astimezone(ZoneInfo('Europe/Amsterdam')).date():
            return finish(item, 'date_review', 'Controleer ordernummer en oorspronkelijke betaaldatum', 'Betaling dateert van voor de bestelling')
    except (KeyError, TypeError, ValueError):
        return finish(item, 'date_review', 'Besteldatum verifiëren', 'Geen geldige besteldatum beschikbaar')
    if order.get('status') not in ('completed', 'processing', 'on-hold', 'pending'):
        return finish(item, 'order_status_review', 'Controleer annulering en terugbetaling', 'Orderstatus staat gewone matching niet toe')
    if m.amount(order.get('total')) != m.amount(item.get('original_bank_amount', item['amount'])):
        return finish(item, 'order_amount_review', 'Vergelijk order, oorspronkelijke bankbetaling en factuurregels', 'Oorspronkelijke betaling wijkt af van webshopordertotaal')
    if item['status'] == 'order_evidence_needed':
        if item['gl_account'] == '1100':
            return finish(item, 'bacs_match_candidate', 'Letter bestaande bankregel af tegen de genoemde verkoopfactuur na actuele identiteitscontrole',
                'BACS-order, datum, valuta en openstaand bedrag geverifieerd')
        item['order_check'] = 'verified'
    elif item['status'] == 'amount_review':
        item['next_action'] = 'Betaling sluit aan op webshoporder; controleer factuurimport, creditnota’s en eerdere afletteringen'
    return item


async def load_assigned(api, journals, headers, bank_fields):
    """Read only open cashflows in ordinary bank journals, then prove line IDs."""
    ordinary = [j for j in journals.values() if j.get('Type') == 12 and not is_psp({
        'JournalDescription': j.get('Description'), 'BankAccountDescription': j.get('BankAccountDescription'), 'AmountDC': 0})]
    if not ordinary:
        return [], []
    clause = ' or '.join("Journal eq '" + code(j['Code']).replace("'", "''") + "'" for j in ordinary)
    flows = []
    for resource in ('cashflow/Receivables', 'cashflow/Payments'):
        flows.extend(await api.rows(resource, params={'$filter': '(Status ne 50) and (' + clause + ')', '$select': CASH_FIELDS, '$orderby': 'ID'}))
    flows = [f for f in flows if f.get('Status') in (20, 30, 40) and m.amount(f['AmountDC']) != 0]
    entries = sorted({m.guid(f['TransactionEntryID']) for f in flows})
    headers = {**headers, **await load_headers(api, [e for e in entries if e not in headers])}
    banks, txs = [], {}
    for start in range(0, len(entries), 15):
        selection = ' or '.join("EntryID eq guid'" + e + "'" for e in entries[start:start + 15])
        banks.extend(await api.rows('financialtransaction/BankEntryLines', params={'$filter': selection, '$select': bank_fields}))
        for t in await api.rows('financialtransaction/TransactionLines', params={'$filter': selection,
                '$select': 'ID,EntryID,LineNumber,Account,GLAccountCode,AmountDC'}):
            txs[code(t['ID']).lower()] = t
    result, unresolved = [], []
    used = set()
    for f in flows:
        tx = txs.get(code(f['TransactionID']).lower())
        found = [b for b in banks if tx and code(b.get('EntryID')).lower() == code(f['TransactionEntryID']).lower()
            and code(b.get('EntryID')).lower() == code(tx['EntryID']).lower()
            and b.get('LineNumber') == tx['LineNumber']
            and code(b.get('Account')).lower() == code(f['Account']).lower() == code(tx.get('Account')).lower()
            and code(b.get('GLAccountCode')) == code(f['GLAccountCode']) == code(tx.get('GLAccountCode'))
            and abs(m.amount(b['AmountDC'])) == abs(m.amount(tx['AmountDC']))]
        if len(found) != 1 or code(found[0]['ID']).lower() in used:
            unresolved.append({'cashflow_id': f['ID'], 'entry_number': f.get('EntryNumber'),
                'account_code': code(f.get('AccountCode')), 'reason': 'Open cashflow niet uniek aan bankregel gekoppeld'})
            continue
        b = enrich_bank(found[0], headers, journals)
        b.update(OpenAmountDC=str(f['AmountDC']), OpenAmountFC=str(f['AmountFC']), OpenEvidence='cashflow_status_' + str(f['Status']), CashflowID=f['ID'])
        if abs(m.amount(f['AmountDC'])) > abs(m.amount(b['AmountDC'])) or f['Currency'] != b.get('Currency'):
            unresolved.append({'cashflow_id': f['ID'], 'entry_number': f.get('EntryNumber'), 'reason': 'Open bankbedrag of valuta inconsistent'})
            continue
        result.append(b)
        used.add(code(b['ID']).lower())
    # Multiple cashflow terms for one line require separate term-level matching.
    blocked = {code(txs.get(code(f['TransactionID']).lower(), {}).get('EntryID')).lower()
        for f in flows if any(u['cashflow_id'] == f['ID'] for u in unresolved)}
    return [b for b in result if code(b['EntryID']).lower() not in blocked], unresolved
