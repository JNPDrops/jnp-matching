"""Presentation of the complete bank review; no financial actions or approvals."""
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from operations import tax_reference as tax


LABELS = {
    'bacs_match_candidate': 'Matchkandidaat · bankoverschrijving',
    'supplier_match_candidate': 'Matchkandidaat · leverancier',
    'psp_match_candidate': 'Matchkandidaat · PSP',
    'invoice_missing': 'Verkoopfactuur nog niet gevonden',
    'invoice_not_open': 'Verkoopboeking bestaat · controleer aflettering',
    'supplier_invoice_missing': 'Inkoopfactuur ontbreekt',
    'supplier_invoice_review': 'Inkoopfactuur controleren',
    'supplier_review': 'Factuur of bon nodig',
    'receipt_identity_needed': 'Herkomst ontvangst nodig',
    'suspense_identity_needed': 'Vraagpost · bronbewijs nodig',
    'psp_reference_review': 'PSP-orderreferentie nodig',
    'bank_identity_review': 'Bankregel identificeren',
    'psp_deferred': 'PSP-afhandeling',
    'refund_review': 'Terugbetaling controleren',
    'order_refund_review': 'Refund of creditnota controleren',
    'duplicate_review': 'Mogelijke dubbele betaling',
    'amount_review': 'Bedragverschil controleren',
    'order_amount_review': 'Orderbedrag wijkt af',
    'payment_method_review': 'Betaalroute controleren',
    'account_review': 'Relatie controleren',
    'order_status_review': 'Annulering controleren',
    'multiple_invoices': 'Meerdere mogelijke facturen',
    'multiple_payments_review': 'Meerdere betalingen voor één factuur',
    'supplier_amount_candidate': 'Factuurreferentie bevestigen',
    'currency_review': 'Valuta controleren',
    'invoice_direction_review': 'Factuur of creditnota controleren',
    'date_review': 'Betaaldatum controleren',
    'order_evidence_needed': 'Webshopbewijs nodig',
    'psp_order_evidence_needed': 'PSP-webshopbewijs nodig',
    'tax_review': 'Belastingonderbouwing nodig',
    'tax_rule_ready': 'Belastingregel toepassen',
    'existing_rule_ready': 'Toewijzingsregel toepassen',
    'new_rule_confirmed': 'Toewijzingsregel toepassen',
}

NEEDED = {
    'supplier_review': 'Inkoopfactuur of bon met leverancier, kostenomschrijving en btw; bevestig ook het zakelijke doel.',
    'supplier_invoice_missing': 'De inkoopfactuur van deze leverancier; controleer of deze al geboekt of eerder afgeletterd is.',
    'supplier_invoice_review': 'Factuurnummer of creditnotanummer, factuurbedrag en eerdere afletteringen bij deze leverancier.',
    'supplier_amount_candidate': 'Betalingsbewijs met factuurnummer; hetzelfde bedrag alleen bewijst de factuur niet.',
    'receipt_identity_needed': 'Betaalbewijs met betaler en ordernummer of een andere verklaring voor de ontvangst.',
    'suspense_identity_needed': 'PSP-/bankbronbestand met transactiereferentie, bronorder en onderbouwing voor de tegenrekening.',
    'psp_reference_review': 'PSP-transactie-ID en de daarbij behorende order uit het betaalbestand.',
    'invoice_missing': 'De verkoopfactuur van deze bronorder, plus verzend-/orderstatus en eerdere afletteringen.',
    'invoice_not_open': 'De bestaande Exact-verkoopboeking en de daaraan gekoppelde betalingen/credits; controleer ook of deze nog in concept staat.',
    'duplicate_review': 'Oorspronkelijk bankafschrift en unieke banktransactie-ID van beide regels; controleer dubbele import.',
    'refund_review': 'Oorspronkelijke ontvangst, reden van terugbetaling en bijbehorende creditnota of bewijs van dubbele betaling.',
    'order_refund_review': 'Refundtransactie, oorspronkelijke betaling en bijbehorende creditnota.',
    'order_status_review': 'Bevestiging van annulering, eventuele creditnota en bewijs of de klant al is terugbetaald.',
    'amount_review': 'Factuur, ordertotaal, deelbetalingen en credits; een resterend verschil vraagt een afzonderlijke beslissing.',
    'order_amount_review': 'Oorspronkelijke orderregels en betaling, eventuele orderwijziging, extra betaling of terugbetaling.',
    'payment_method_review': 'Betaalbewijs en PSP-transactie bij de bronorder om de juiste betaalroute en debiteur te bevestigen.',
    'account_review': 'Bewijs dat bankbetaling en factuur bij dezelfde order/leverancier en debiteur/crediteur horen.',
    'bank_identity_review': 'Exact-boekingsregels en eventuele splitsingen/deelafletteringen van deze open bankpost.',
    'psp_deferred': 'PSP-afrekening met transacties, kosten en uitbetaling; werk dit af via het eigen PSP-bankboek en de tussenrekening.',
    'multiple_payments_review': 'Alle betaalbewijzen voor deze bronorder, inclusief unieke transactie-ID en eventuele dubbele import.',
}


def decorate(item, observed_at=None):
    item = dict(item)
    state = item.get('status', 'bank_identity_review')
    candidate = state.endswith('_match_candidate')
    processing = state == 'invoice_missing' and item.get('order_status') in ('processing', 'pending', 'on-hold')
    group = 'candidate' if candidate else ('waiting' if processing else 'review')
    item.update(status_label=LABELS.get(state, 'Beoordeling nodig'), work_group=group,
        division=item.get('division', 3977752),
        needed_information=NEEDED.get(state, 'Controleer de vermelde brongegevens en voer de aangegeven vervolgstap uit.'),
        action_owner='Boekhouding', execution_label='Afhandeling nog niet bevestigd',
        observed_at=observed_at or item.get('observed_at'))
    if candidate:
        item['needed_information'] = 'Actuele Exact-identiteiten en open saldi van deze betaling en deze specifieke factuur vlak vóór aflettering.'
    if processing:
        item['needed_information'] = 'Verzending en beschikbaarheid van de factuur van ' + str(item.get('reference') or 'deze order') + ' in Exact.'
        item['action_owner'] = 'Orderverwerking / factuurimport'
    if state in ('order_evidence_needed', 'psp_order_evidence_needed') or item.get('order_check') == 'order_not_found':
        item['needed_information'] += ' Actueel webshopbewijs is nog niet bevestigd; controleer de bronorder en probeer opnieuw.'
    if item.get('existing_sales_entries'):
        item['needed_information'] += ' Bestaande verkoopboekingen: ' + ', '.join(str(x['EntryNumber']) for x in item['existing_sales_entries']) + '.'
    item.setdefault('next_action', 'Identificeer deze betaling en controleer de bijbehorende factuur of brononderbouwing')
    item.setdefault('retry_when', 'na aanvulling van het gevraagde bewijs')
    try:
        item['bank_date_display'] = tax.bank_date(item.get('bank_date')).isoformat()
    except (TypeError, ValueError, KeyError):
        item['bank_date_display'] = 'Onbekend'
    return item


def report_data(status, items, identities):
    rows = [decorate(x) for x in items] + [decorate(x) for x in identities]
    priority = {'candidate': 0, 'review': 1, 'waiting': 2}
    rows.sort(key=lambda x: (priority[x['work_group']], x['status_label'], x.get('bank_date_display', ''), str(x.get('entry_number', ''))))
    counts = Counter(x['work_group'] for x in rows)
    stale = True
    try:
        observed = datetime.fromisoformat(status['last_scan'])
        stale = (datetime.now(timezone.utc) - observed).total_seconds() > 7200
        scanned_at = observed.astimezone(ZoneInfo('Europe/Amsterdam')).strftime('%d-%m-%Y %H:%M')
    except (KeyError, TypeError, ValueError):
        scanned_at = 'Nog geen volledige controle'
    return dict(status=status, items=rows, totals=dict(counts), total=len(rows), scanned_at=scanned_at,
        stale=stale, classifications=sorted({(x['status'], x['status_label']) for x in rows}))
