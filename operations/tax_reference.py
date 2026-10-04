"""Local Belastingdienst reference decoding, without network calls or writes.

Source: Belastingdienst Specificatie Betalingskenmerk bepaling v1.5.
Only V, A, B, F and L have supported reversible encodings here. Random
references, other tax types and incomplete references are review cases.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re

RSIN = '867393051'
TAX_ACCOUNT_ID = '6659f075-bdcc-4c6f-a4ca-89b836aca6ec'
# Counterparty accounts observed in this administration, never the own SWAN IBAN.
TAX_IBANS = frozenset(('NL04RABO0200112244', 'NL86INGB0002445588',
                       'NL36INGB0003445588', 'NL88INGB0000441047'))
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
REFUND_ASSESSMENT = re.compile(
    r'(?<![A-Za-z0-9])(?P<rsin>[0-9]{9}|[0-9]{4}\.[0-9]{2}\.[0-9]{3})'
    r'\.?\s*O\.?\s*(?P<sub>[0-9]{2})\.?(?P<year>[0-9])(?P<period>[0-9]{2})(?P<seq>[0-9])(?![A-Za-z0-9])', re.I)


def tax_hint(bank, expected_rsin=RSIN):
    """Reserve known tax counterparties/references before any debtor matching."""
    text = ' '.join(str(bank.get(k) or '') for k in
                    ('Description', 'PaymentReference', 'YourRef', 'AccountName', 'AccountBankAccount'))
    compact = re.sub(r'[ .\t\u00a0-]', '', text).upper()
    return (bool(re.search(r'belastingdienst', text, re.I))
            or str(bank.get('Account') or '').lower() == TAX_ACCOUNT_ID
            or str(bank.get('AccountCode') or '').strip() == '1'
            or expected_rsin[:8] in compact or expected_rsin[2:8] in compact
            or any(iban in compact for iban in TAX_IBANS))


def vat_refund_decision(bank, expected_rsin=RSIN):
    """Operator-approved ordinary BTW refunds, including observed OB quarter text.

    O is a refund assessment, not a generated payment reference. Keep the
    complete observed narrative as the rule criterion; never generate O numbers.
    """
    text = str(bank.get('Description') or '').strip()
    named = bool(re.search(r'belastingdienst', str(bank.get('AccountName') or '') + ' ' + text, re.I))
    linked = str(bank.get('Account') or '').lower() == TAX_ACCOUNT_ID
    if not (named or linked) or EXTRAS.search(text) or re.search(r'\boss\b|loonheffing|loonbelasting|vennootschap|\bvpb\b', text, re.I):
        return None
    if not re.search(r'\b(?:teruggaaf|teruggave|restitutie|refund)\b', text, re.I):
        return None
    try:
        amount = Decimal(str(bank.get('AmountDC')))
        anchor = bank_date(bank.get('Date')).year
        if not amount.is_finite() or amount <= 0:
            return None
        refs = list(REFUND_ASSESSMENT.finditer(text))
        quarter = re.findall(r'\bOB\.?\s*([1-4])E?\s*KWART(?:AAL)?\s*([0-9]{2}|[0-9]{4})(?![0-9])', text, re.I)
        explicit = bool(re.search(r'omzetbelasting|\bbtw\b', text, re.I))
        if not explicit and not (len(refs) == 1 and len(quarter) == 1):
            return None
        if len(refs) > 1 or len(quarter) > 1 or not 15 <= len(text) <= 240:
            return None
        result = {'tax_letter': 'B', 'tax_year': anchor, 'year_inferred': False,
                  'subnumber': None, 'period_code': None, 'assessment_number': None}
        if refs:
            ref = refs[0]
            rsin = ref['rsin'].replace('.', '')
            if rsin != expected_rsin or complete_rsin(rsin[:8]) != rsin or ref['sub'] == '00' or ref['seq'] != '0':
                return None
            if ref['period'] not in tuple(f'{i:02}' for i in range(1, 13)) + ('21', '24', '27', '30'):
                return None
            year = nearest_year(ref['year'], anchor)
            if quarter and (ref['period'] != ('21', '24', '27', '30')[int(quarter[0][0])-1]
                            or year % 100 != int(quarter[0][1]) % 100):
                return None
            result.update(tax_letter='O', tax_year=year, year_inferred=True,
                          subnumber=ref['sub'], period_code=ref['period'], assessment_number=ref.group())
        decoded = []
        for pattern, decoder in ((PAYMENT_PATTERN, decode_payment), (ASSESSMENT_PATTERN, decode_assessment)):
            for match in pattern.finditer(text):
                d = decoder(match.group(), anchor_year=anchor, expected_rsin=expected_rsin)
                if d['tax_bucket'] != 'btw' or d['needs_assessment_split']:
                    return None
                decoded.append(d)
        if len({d['payment_reference'] for d in decoded}) > 1 or (refs and decoded):
            return None
        if decoded:
            result.update(decoded[0])
    except (ReferenceError, InvalidOperation, ValueError, TypeError):
        return None
    return {**result, 'status': 'TAX_IDENTIFIED', 'tax_bucket': 'btw', 'tax_type': 'Omzetbelasting teruggaaf',
            'rsin': expected_rsin, 'direction': 'incoming', 'amount_dc': str(bank.get('AmountDC')),
            'assessment_kind': 'teruggaaf', 'reference_source': 'explicit_refund_description',
            'allocation_words': text, 'needs_assessment_split': False, 'review_reasons': [],
            'booking_executed': False, 'read_only': True,
            'reason': 'Expliciete btw-teruggaaf; operatorbeleid: rechtstreeks naar 1770, geen debiteur of crediteur'}


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
    named = tax_hint(bank, expected_rsin)
    own_hint = expected_rsin[:8] in text or expected_rsin[2:8] in text or expected_rsin in text.replace('.', '')
    payments = [m.group() for value in fields for m in PAYMENT_PATTERN.finditer(value)]
    assessments = [m.group() for value in fields for m in ASSESSMENT_PATTERN.finditer(value)]
    if not named and not own_hint and not payments and not assessments:
        return None
    refund = vat_refund_decision(bank, expected_rsin)
    if refund:
        return refund
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
