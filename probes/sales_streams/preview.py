"""Offline debtor-routing experiment. No network, credentials or accounting writes."""
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


def money(value):
    if value is None or isinstance(value, bool):
        raise ValueError('missing/invalid amount')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid amount') from exc
    if not result.is_finite() or result != result.quantize(Decimal('0.01')):
        raise ValueError('amount must be finite with at most two decimals')
    return result


def preview(orders, mapping):
    if not isinstance(orders, list) or not isinstance(mapping, dict):
        raise ValueError('orders must be a list; mapping must be an object')
    if any(not isinstance(o, dict) for o in orders):
        raise ValueError('each order must be an object')
    ids = Counter(str(o.get('id')) for o in orders)
    numbers = Counter(str(o.get('number')) for o in orders)
    rows = []
    for o in orders:
        issues = []
        oid, number = o.get('id'), o.get('number')
        code = o.get('payment_method')
        route = mapping.get(code) if isinstance(code, str) else None
        if not isinstance(route, dict):
            route = {}
        if oid is None or not str(oid).isdigit():
            issues.append('INVALID_ORDER_ID')
        if not isinstance(number, str) or not number.isdigit():
            issues.append('ORDER_NUMBER_REQUIRES_REVIEW')
        if ids[str(oid)] > 1 or numbers[str(number)] > 1:
            issues.append('DUPLICATE_ORDER_ID_OR_NUMBER')
        if not route:
            issues.append('UNKNOWN_PAYMENT_METHOD')
        debtor = route.get('debtor_code')
        if not isinstance(debtor, str) or not debtor.strip() or not route.get('stream'):
            issues.append('ROUTING_NOT_CONFIGURED')
        if route.get('confirmed') is not True:
            issues.append('ROUTING_NOT_CONFIRMED')
        if o.get('status') not in ('processing', 'completed'):
            issues.append('ORDER_STATUS_REQUIRES_REVIEW')
        if o.get('currency') != 'EUR':
            issues.append('CURRENCY_REQUIRES_REVIEW')
        amount = None
        try:
            amount = money(o.get('total'))
            if amount <= 0:
                issues.append('NON_POSITIVE_TOTAL')
        except ValueError:
            issues.append('INVALID_TOTAL')
        # Refund summaries are not a substitute for complete credit records.
        if 'refunds' not in o or not isinstance(o.get('refunds'), list):
            issues.append('REFUND_INFORMATION_MISSING')
        elif o['refunds']:
            issues.append('REFUND_REQUIRES_CREDIT_REVIEW')
        rows.append({
            'order_id': oid if isinstance(oid, int) else None,
            'order_number': number if isinstance(number, str) and number.isdigit() else None,
            'payment_method': code if isinstance(code, str) else None,
            'currency': o.get('currency'),
            'gross_total': str(amount) if amount is not None else None,
            'proposed_stream': route.get('stream'),
            'proposed_debtor_code': debtor,
            'routing_status': 'REVIEW' if issues else 'ROUTING_PROPOSAL',
            'issues': issues,
            'exact_write_allowed': False,
            'exact_comparison': 'NOT_PERFORMED',
        })
    return {
        'mode': 'OFFLINE_READ_ONLY', 'financial_writes': 0,
        'scope': 'Supplied records only; not proof of complete shop coverage.',
        'qualification': 'Routing proposals only; VAT, accounting, settlements and matches not verified.',
        'method_counts': dict(Counter(r['payment_method'] or '<missing>' for r in rows)),
        'review_count': sum(r['routing_status'] == 'REVIEW' for r in rows),
        'orders': rows,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--orders', required=True, type=Path)
    p.add_argument('--mapping', required=True, type=Path)
    args = p.parse_args()
    try:
        result = preview(json.loads(args.orders.read_text()), json.loads(args.mapping.read_text()))
    except (ValueError, OSError):
        p.exit(2, 'Invalid/unreadable input. Check JSON structure; input contents are not logged.\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
