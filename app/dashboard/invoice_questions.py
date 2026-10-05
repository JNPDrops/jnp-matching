"""Display-only invoice groups. Order status never authorizes financial work."""
GROUPS = {
    'purchase_missing': 'Ontbrekende inkoopfacturen',
    'purchase_review': 'Inkoopfactuur of bon controleren',
    'sales_waiting': 'Webshoporder wacht op factuurimport',
    'sales_review': 'Verkoopfactuur / order controleren',
    'other': 'Overige vragen',
}
SALES_MISSING = {'invoice_missing', 'invoice_missing_or_ambiguous'}


def annotate(item, *, order=None, checked_at=None, lookup_state=None,
             invoice_absent=False, expected_order_id=None):
    category = item['category']
    group = ('purchase_missing' if category == 'supplier_invoice_missing' else
             'purchase_review' if category.startswith('supplier_') else
             'sales_review' if 'invoice' in category else 'other')
    if category in SALES_MISSING:
        order = order or {}
        reference = item.get('reference') or ''
        matches = (reference.startswith('TD') and reference[2:].isdigit()
                   and order.get('order_number') == '#' + reference[2:]
                   and type(order.get('order_id')) is int and order['order_id'] > 0
                   and (not expected_order_id or str(order['order_id']) == str(expected_order_id)))
        status = order.get('status') if matches else None
        state, note = 'unchecked', 'Webshopstatus nog niet bevestigd; een ontbrekende open post bewijst niet dat de factuur ontbreekt.'
        if lookup_state == 'unavailable':
            state, note = 'unavailable', 'Webshopcontrole niet beschikbaar; opnieuw controleren.'
        elif lookup_state == 'not_found':
            state, note = 'not_found', 'Deze order is niet gevonden in de gecontroleerde webshopbron.'
        elif matches:
            if status == 'processing' and invoice_absent:
                group = 'sales_waiting'
                state, note = 'awaiting_import', 'Order staat op processing. Controleer verzending en Xcore-import; de verkoopfactuur is bij de laatste Exact-controle niet aangetroffen. Import is nog niet bevestigd.'
            elif status in {'pending', 'on-hold'}:
                state, note = 'order_on_hold', 'Order wacht op betaling of vrijgave. Controleer de order; toekomstige factuurimport is nog niet zeker.'
            elif status == 'completed':
                state, note = 'completed_check_import', 'Order is completed. Controleer de Xcore-import, bestaande verkoopboekingen en eerdere afletteringen.'
            elif status in {'cancelled', 'failed', 'refunded'}:
                state, note = 'order_status_review', 'Controleer annulering, betaling, terugbetaling en eventuele creditnota.'
            else:
                state, note = 'check_invoice_history', 'Webshoporder gevonden. Controleer de Exact-verkoopboeking en eerdere afletteringen; import is niet bevestigd.'
        elif order:
            state, note = 'identity_mismatch', 'Webshopidentiteit wijkt af of is onvolledig; orderkoppeling handmatig controleren.'
        item['order_import'] = {'state': state, 'status': status,
                                'checked_at': checked_at, 'note': note}
    item['question_group'] = group
    item['question_group_label'] = GROUPS[group]
    return item
