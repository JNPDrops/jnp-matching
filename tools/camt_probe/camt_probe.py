"""Generate synthetic CAMT.053.001.02 mapping probes; never contact Exact.

Local format checks are not proof of acceptance or matching by Exact Online.
The receiving IBAN, opening balance and unused statement number must be supplied.
The default amount is EUR 0.01. Each run creates ONE file, never imports it.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import xml.etree.ElementTree as ET

NS = 'urn:iso:std:iso:20022:tech:xsd:camt.053.001.02'
VARIANTS = ('control', 'structured', 'end_to_end', 'unstructured')
ET.register_namespace('', NS)


def iban_check(value: str) -> str:
    value = re.sub(r'\s+', '', value).upper()
    if not re.fullmatch(r'[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}', value):
        raise ValueError('A valid receiving IBAN is required; no placeholder accepted.')
    digits = ''.join(str(ord(c) - 55) if c.isalpha() else c for c in value[4:] + value[:4])
    if int(digits) % 97 != 1:
        raise ValueError('IBAN checksum is invalid.')
    return value


def amount_check(value: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal('999999999999.99'):
            raise ValueError('Invalid monetary value.')
        if number != number.quantize(Decimal('0.01')):
            raise ValueError('Amounts must have at most two decimal places.')
    except InvalidOperation as exc:
        raise ValueError('Invalid monetary value.') from exc
    if positive and not Decimal('0.01') <= number <= Decimal('1.00'):
        raise ValueError('This mapping probe is limited to EUR 0.01 through EUR 1.00.')
    return number


def elem(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, '{' + NS + '}' + tag, attrs)
    if text is not None:
        node.text = text
    return node


def balance(parent: ET.Element, code: str, value: Decimal, day: str) -> None:
    node = elem(parent, 'Bal')
    elem(elem(elem(node, 'Tp'), 'CdOrPrtry'), 'Cd', code)
    elem(node, 'Amt', f'{abs(value):.2f}', Ccy='EUR')
    elem(node, 'CdtDbtInd', 'CRDT' if value >= 0 else 'DBIT')
    elem(elem(node, 'Dt'), 'Dt', day)


def build_probe(*, iban: str, opening_balance: str, statement_number: int,
                booking_date: str, run_id: str, variant: str,
                amount: str = '0.01') -> bytes:
    """Produce one synthetic statement with exactly one credit entry.

    The reference under investigation appears only in its candidate field.
    Transport/statement IDs necessarily differ to distinguish separate imports.
    The control has no candidate reference. No real customer reference is used.
    """
    iban = iban_check(iban)
    opening = amount_check(opening_balance)
    value = amount_check(amount, positive=True)
    date.fromisoformat(booking_date)
    if not re.fullmatch(r'[A-Z0-9]{4,12}', run_id):
        raise ValueError('run_id must contain 4-12 uppercase letters or digits.')
    if variant not in VARIANTS:
        raise ValueError('Unknown probe variant.')
    if not isinstance(statement_number, int) or not 1 <= statement_number <= 999999999:
        raise ValueError('Use a positive, unused statement number up to 999999999.')
    ref = 'JNPTEST-MAP-' + run_id
    identifier = 'JNPTEST-' + run_id + '-' + str(VARIANTS.index(variant))
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    root = ET.Element('{' + NS + '}Document')
    root.append(ET.Comment('SYNTHETIC JNP IMPORT TEST - NOT A BANK-ISSUED STATEMENT'))
    report = elem(root, 'BkToCstmrStmt')
    group = elem(report, 'GrpHdr')
    elem(group, 'MsgId', identifier)
    elem(group, 'CreDtTm', stamp)
    stmt = elem(report, 'Stmt')
    elem(stmt, 'Id', identifier)
    elem(stmt, 'ElctrncSeqNb', str(statement_number))
    elem(stmt, 'CreDtTm', stamp)
    account = elem(stmt, 'Acct')
    elem(elem(account, 'Id'), 'IBAN', iban)
    elem(account, 'Ccy', 'EUR')
    balance(stmt, 'OPBD', opening, booking_date)
    balance(stmt, 'CLBD', opening + value, booking_date)
    entry = elem(stmt, 'Ntry')
    elem(entry, 'NtryRef', identifier)
    elem(entry, 'Amt', f'{value:.2f}', Ccy='EUR')
    elem(entry, 'CdtDbtInd', 'CRDT')
    elem(entry, 'Sts', 'BOOK')
    elem(elem(entry, 'BookgDt'), 'Dt', booking_date)
    elem(elem(entry, 'ValDt'), 'Dt', booking_date)
    elem(entry, 'AcctSvcrRef', identifier)
    domain = elem(elem(entry, 'BkTxCd'), 'Domn')
    elem(domain, 'Cd', 'PMNT')
    family = elem(domain, 'Fmly')
    elem(family, 'Cd', 'RCDT')
    elem(family, 'SubFmlyCd', 'ESCT')
    tx = elem(elem(entry, 'NtryDtls'), 'TxDtls')
    refs = elem(tx, 'Refs')
    elem(refs, 'EndToEndId', ref if variant == 'end_to_end' else 'NOTPROVIDED')
    elem(refs, 'TxId', identifier)
    elem(elem(elem(tx, 'AmtDtls'), 'TxAmt'), 'Amt', f'{value:.2f}', Ccy='EUR')
    elem(elem(elem(tx, 'RltdPties'), 'Dbtr'), 'Nm', 'JNP SYNTHETIC TEST')
    if variant == 'structured':
        # No SCOR/ISO type claim: the sentinel is not an ISO 11649 RF reference.
        elem(elem(elem(elem(tx, 'RmtInf'), 'Strd'), 'CdtrRefInf'), 'Ref', ref)
    elif variant == 'unstructured':
        elem(elem(tx, 'RmtInf'), 'Ustrd', ref)
    elem(tx, 'AddtlTxInf', 'JNP SYNTHETIC TEST - ' + identifier)
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iban', required=True)
    parser.add_argument('--opening-balance', required=True)
    parser.add_argument('--statement-number', required=True, type=int)
    parser.add_argument('--booking-date', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--variant', required=True, choices=VARIANTS)
    parser.add_argument('--amount', default='0.01')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        data = build_probe(iban=args.iban, opening_balance=args.opening_balance,
                           statement_number=args.statement_number,
                           booking_date=args.booking_date, run_id=args.run_id,
                           variant=args.variant, amount=args.amount)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('xb') as handle:
            handle.write(data)
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(f'Created {args.output}; not imported into Exact. Acceptance is unverified.')


if __name__ == '__main__':
    main()
