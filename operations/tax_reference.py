"""Local Belastingdienst reference decoding, without network calls or writes.

Source: Belastingdienst Specificatie Betalingskenmerk bepaling v1.5.
Only V, A, B, F and L have supported reversible encodings here. Random
references, other tax types and incomplete references are review cases.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re

RSIN = '867393051'
WEIGHTS = (2, 4, 8, 5, 10, 9, 7, 3, 6, 1)
NAMES = {'V': 'Vennootschapsbelasting', 'B': 'Omzetbelasting',
         'L': 'Loonheffingen', 'F': 'Naheffing omzetbelasting',
         'A': 'Naheffing loonheffingen'}
BUCKETS = {'V': 'vpb', 'B': 'btw', 'F': 'btw', 'L': 'loonheffingen', 'A': 'loonheffingen'}
# Do not join separate unrelated digit runs into a seemingly valid reference.
PAYMENT_PATTERN = re.compile(r'(?<![A-Za-z0-9])(?:[0-9]{16}|[0-9]{4}(?:[ .\t\u00a0-][0-9]{4}){3})(?![A-Za-z0-9])')
ASSESSMENT_PATTERN = re.compile(
    r'(?<![A-Za-z0-9])(?P<rsin>[0-9]{9}|[0-9]{4}\.[0-9]{2}\.[0-9]{3})'
    r'\.?\s*(?P<letter>[VABFL])\.?\s*(?P<tail>[0-9]{2,3}\.?[0-9]{4})(?![A-Za-z0-9])', re.I)
EXTRAS = re.compile(r'boete|rente|aanman|dwangbevel|invordering|betalingsregeling|verreken|verzamel|corona|kosten', re.I)


class ReferenceError(ValueError):
    pass


def check_digit(body):
    if not isinstance(body, str) or not re.fullmatch(r'[0-9]{15}', body):
        raise ReferenceError('Ongeldige lengte betalingskenmerk')
    value = 11 - sum(int(c) * WEIGHTS[i % 10] for i, c in enumerate(reversed(body))) % 11
    return str({10: 1, 11: 0}.get(value, value))


def complete_rsin(first_eight):
    if not re.fullmatch(r'[0-9]{8}', first_eight):
        raise ReferenceError('Ongeldig RSIN')
    last = sum(int(c) * (9 - i) for i, c in enumerate(first_eight)) % 11
    if last == 10:
        raise ReferenceError('RSIN-controle mislukt')
    return first_eight + str(last)


def nearest_year(digit, anchor_year):
    """Operator policy, 2026-10-04. At an exact five-year tie use the earlier year."""
    if type(anchor_year) is not int or not 1000 <= anchor_year <= 9999 or not re.fullmatch(r'[0-9]', str(digit)):
        raise ReferenceError('Ongeldig jaar')
    base = anchor_year // 10 * 10 + int(digit)
    return min((base - 10, base, base + 10), key=lambda y: (abs(y - anchor_year), y))


def bank_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        match = re.fullmatch(r'/Date\((-?[0-9]+)(?:[+-][0-9]{4})?\)/', value)
        if match:
            return datetime.fromtimestamp(int(match[1]) / 1000, timezone.utc).date()
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    raise ReferenceError('Bankdatum ontbreekt of is ongeldig')


def decode_payment(value, *, anchor_year, expected_rsin=RSIN):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9 .\t\u00a0-]+', value):
        raise ReferenceError('Ongeldig betalingskenmerk')
    number = re.sub(r'[ .\t\u00a0-]', '', value)
    if len(number) != 16 or check_digit(number[1:]) != number[0]:
        raise ReferenceError('Controlecijfer betalingskenmerk klopt niet')
    code = int(number[9:11])
    if code == 74 or 80 <= code <= 84 or 92 <= code <= 96:
        prefix = '00' if code == 74 else str(code if code <= 84 else code - 7)
        rsin = complete_rsin(prefix + number[1:7])
        letter, digit, kind, period = 'V', number[7], number[8], number[11:15]
        if number[15] != '0':
            raise ReferenceError('Onbekende Vpb-middelherkenning')
        start, end = int(period[:2]), int(period[2:])
        if not 1 <= start <= 12 or not start <= end <= 24:
            raise ReferenceError('Onbekend Vpb-tijdvak')
        tail = digit + kind + period
        assessment_kind = 'voorlopig' if int(kind) <= 5 else ('definitief' if kind == '6' else 'navordering')
        split_review = int(kind) >= 6
        subnumber, sequence = None, None
    elif number[9] in '0156':
        rsin = complete_rsin(number[1:9])
        letter = {'0': 'A', '1': 'B', '5': 'F', '6': 'L'}[number[9]]
        digit, subnumber, period, sequence = number[10], number[11:13], number[13:15], number[15]
        if subnumber == '00' or int(period) == 0:
            raise ReferenceError('Onbekend subnummer of tijdvak')
        tail = subnumber + digit + period + sequence
        assessment_kind = 'naheffing' if letter in 'AF' else 'aangifte'
        split_review = letter in 'AF'
    else:
        raise ReferenceError('Belastingsoort niet ondersteund of willekeurig kenmerk')
    if expected_rsin is not None and rsin != expected_rsin:
        raise ReferenceError('RSIN behoort niet tot James n Parson')
    year = nearest_year(digit, anchor_year)
    formatted_rsin = f'{rsin[:4]}.{rsin[4:6]}.{rsin[6:]}'
    return {'payment_reference': number, 'rsin': rsin, 'tax_letter': letter,
            'tax_type': NAMES[letter], 'tax_bucket': BUCKETS[letter],
            'tax_year': year, 'year_inferred': True, 'year_anchor': anchor_year,
            'year_policy': 'nearest_to_bank_year_tie_earlier', 'period_code': period,
            'assessment_kind': assessment_kind, 'subnumber': subnumber, 'sequence': sequence,
            'assessment_number': f'{formatted_rsin}.{letter}.{tail[:2]}.{tail[2:]}',
            'needs_assessment_split': split_review}


def decode_assessment(value, *, anchor_year, expected_rsin=RSIN):
    match = ASSESSMENT_PATTERN.fullmatch(value.strip())
    if not match:
        raise ReferenceError('Onbekende opbouw aanslagnummer')
    rsin, letter, tail = match['rsin'].replace('.', ''), match['letter'].upper(), match['tail'].replace('.', '')
    if complete_rsin(rsin[:8]) != rsin:
        raise ReferenceError('RSIN-controle mislukt')
    # Official unformatted numbers carry two year digits; printed numbers one.
    if len(tail) == 7:
        offset = 0 if letter == 'V' else 2
        explicit_year = tail[offset:offset + 2]
        if nearest_year(explicit_year[-1], anchor_year) % 100 != int(explicit_year):
            raise ReferenceError('Expliciet jaar botst met dichtstbijzijnde jaar')
        tail = tail[:offset] + tail[offset + 1:]
    if len(tail) != 6:
        raise ReferenceError('Onbekende opbouw aanslagnummer')
    if letter == 'V':
        prefix = int(rsin[:2])
        if prefix == 0: code = 74
        elif 80 <= prefix <= 84: code = prefix
        elif 85 <= prefix <= 89: code = prefix + 7
        else: raise ReferenceError('Ongeldige RSIN-prefix voor Vpb')
        body = rsin[2:8] + tail[:2] + str(code) + tail[2:] + '0'
    else:
        body = rsin[:8] + {'A': '0', 'B': '1', 'F': '5', 'L': '6'}[letter] + tail[2] + tail[:2] + tail[3:]
    result = decode_payment(check_digit(body) + body, anchor_year=anchor_year, expected_rsin=expected_rsin)
    result['reference_source'] = 'assessment_number'
    return result


def classify_bank_line(bank, *, expected_rsin=RSIN):
    """Return None for unrelated lines; never infer a tax from name/IBAN alone.

    AmountDC > 0 is incoming in the existing BankEntryLines flow. The original
    signed amount is retained. This decision is a proposal, never a booking.
    """
    fields = [str(bank.get(k) or '') for k in ('Description', 'PaymentReference', 'YourRef')]
    text = ' | '.join(fields)
    named = bool(re.search(r'belastingdienst', str(bank.get('AccountName') or '') + ' ' + text, re.I))
    own_hint = expected_rsin[:8] in text or expected_rsin[2:8] in text or expected_rsin in text.replace('.', '')
    payments = [m.group() for value in fields for m in PAYMENT_PATTERN.finditer(value)]
    assessments = [m.group() for value in fields for m in ASSESSMENT_PATTERN.finditer(value)]
    if not named and not own_hint and not payments and not assessments:
        return None
    result = {'status': 'REVIEW_TAX', 'reason': '', 'booking_executed': False, 'read_only': True,
              'direction': None, 'amount_dc': str(bank.get('AmountDC')), 'review_reasons': []}
    try:
        anchor = bank_date(bank.get('Date')).year
    except ReferenceError as exc:
        if not named and not own_hint: return None
        result['reason'] = str(exc)
        return result
    decoded, errors = {}, []
    for value, decoder in [(x, decode_payment) for x in payments] + [(x, decode_assessment) for x in assessments]:
        try:
            d = decoder(value, anchor_year=anchor, expected_rsin=expected_rsin)
            decoded[d['payment_reference']] = d
        except ReferenceError as exc:
            errors.append(str(exc))
    if not decoded and not named and not own_hint:
        return None
    if not decoded:
        result['reason'] = '; '.join(sorted(set(errors))) or 'Geen volledig ondersteund betalingskenmerk of aanslagnummer'
        return result
    if len(decoded) != 1:
        result['reason'] = 'Meerdere aanslagen in één bankbetaling; splitsing nodig'
        return result
    result.update(next(iter(decoded.values())))
    if errors:
        result['review_reasons'].append('Aanvullend ongeldig of afwijkend kenmerk in dezelfde betaling')
    try:
        amount = Decimal(str(bank.get('AmountDC')))
        if not amount.is_finite() or amount == 0: raise InvalidOperation()
        result['direction'] = 'incoming' if amount > 0 else 'outgoing'
    except (InvalidOperation, ValueError, TypeError):
        result['review_reasons'].append('Geen geldig niet-nul bankbedrag')
    if EXTRAS.search(text) or result['needs_assessment_split']:
        result['review_reasons'].append('Aanslag of specificatie nodig voor rente, boetes, kosten of verrekening')
    # Incoming refunds may include interest without an explicit description.
    if result['direction'] == 'incoming':
        result['review_reasons'].append('Controleer teruggaafbeschikking op rente en verrekening')
    if result['review_reasons']:
        result['reason'] = '; '.join(result['review_reasons'])
    else:
        result.update(status='TAX_IDENTIFIED', reason='RSIN, belastingsoort, jaar en kenmerk herkend; rekeningtoewijzing nog niet uitgevoerd')
    return result


def account_candidates(accounts):
    """Suggest existing balance accounts only; never auto-select by fuzzy name."""
    patterns = {'vpb': r'vennootschap|\bvpb\b', 'btw': r'omzetbelasting|\bbtw\b',
                'loonheffingen': r'loonheffing|loonbelasting'}
    return {bucket: [{'code': str(a.get('Code') or '').strip(), 'description': a.get('Description'),
                      'id': a.get('ID'), 'type': a.get('Type'), 'vat_code': a.get('VATCode')}
                     for a in accounts if a.get('BalanceType') == 'B' and a.get('IsBlocked') is False
                     and re.search(pattern, str(a.get('Description') or ''), re.I)]
            for bucket, pattern in patterns.items()}


def add_account_proposal(decision, accounts, mapping):
    result = dict(decision)
    bucket = result.get('tax_bucket')
    code = mapping.get(bucket)
    candidates = account_candidates(accounts).get(bucket, [])
    result['account_candidates'] = candidates
    result['gl_account_code'] = None
    result['gl_account_id'] = None
    matches = [a for a in accounts if str(a.get('Code') or '').strip() == code] if code else []
    if len(matches) == 1 and matches[0].get('BalanceType') == 'B' and matches[0].get('IsBlocked') is False:
        result.update(gl_account_code=code, gl_account_id=matches[0]['ID'],
                      gl_account_description=matches[0]['Description'])
    else:
        result['reason'] += '; belastingrekening nog niet geverifieerd'
    return result
