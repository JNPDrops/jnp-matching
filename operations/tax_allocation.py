"""Generate complete, RSIN-checked tax references and reconcile Exact rules.

Exact writes are POST AllocationRule and retirement of one verified legacy rule. It does not book bank lines or
invoke Automatically. A durable intent is never blindly posted a second time.
"""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import urlparse

import httpx
from operations import task_drain
from operations import tax_write_operations as writes
from operations import tax_reference as tax
from operations import bacs_debtor_transfer as transport
from operations import allocation_connection as allocation
from operations.woo_iban_rules import ExactAPI as CollectionAPI

ROOT = f'{transport.BASE}/api/v1/beta/{transport.DIVISION}/cashflow/AllocationRule'
STATUS = {'state': 'starting', 'bank_writes': False, 'automatically_executed': False}
PERIODS = {
    'monthly': tuple(f'{i:02}' for i in range(1, 13)),
    'quarterly': ('21', '24', '27', '30'),
    'four_weekly': tuple(str(i) for i in range(71, 84)),
    'half_yearly': ('31', '32'), 'yearly': ('40',),
}


def reference(letter, year, period, *, subnumber=None, kind=None):
    if type(year) is not int or not 2000 <= year <= 2099:
        raise tax.ReferenceError('Ongeldig generatiejaar')
    if letter == 'V':
        if kind not in range(6) or not re.fullmatch(r'[0-9]{4}', period):
            raise tax.ReferenceError('Alleen voorlopige Vpb-kenmerken')
        tail = str(year % 100).zfill(2) + str(kind) + period
    elif letter in ('B', 'L'):
        allowed = set(PERIODS['monthly'] + PERIODS['quarterly']) if letter == 'B' else set(
            sum((PERIODS[k] for k in ('monthly', 'four_weekly', 'half_yearly', 'yearly')), ()))
        if period not in allowed or not isinstance(subnumber, str) or not re.fullmatch(r'0[1-9]|[1-9][0-9]', subnumber):
            raise tax.ReferenceError('Onbekend aangiftetijdvak of subnummer')
        tail = subnumber + str(year % 100).zfill(2) + period + '0'
    else:
        raise tax.ReferenceError('Belastingsoort niet geschikt voor gegenereerde regels')
    decoded = tax.decode_assessment(tax.RSIN + letter + tail, anchor_year=year)
    if decoded['tax_year'] != year or decoded['tax_letter'] != letter or decoded['period_code'] != period:
        raise tax.ReferenceError('Generatiecontrole mislukt')
    return decoded


def templates(policy, decisions):
    result = list(policy['rule_templates'])
    # Only validated ordinary outgoing L references establish a new subnumber
    # and frequency. The existence of a payroll GL account is not sufficient.
    for d in decisions:
        if d.get('tax_letter') != 'L' or d.get('status') != 'TAX_IDENTIFIED' or d.get('direction') != 'outgoing':
            continue
        if not d.get('payment_reference'):
            continue
        check = tax.decode_payment(d['payment_reference'], anchor_year=d['tax_year'])
        if check['tax_letter'] != 'L' or check['subnumber'] != d.get('subnumber'):
            continue
        for frequency in ('monthly', 'four_weekly', 'half_yearly', 'yearly'):
            if check['period_code'] in PERIODS[frequency]:
                item = {'letter': 'L', 'subnumber': check['subnumber'], 'frequency': frequency}
                if item not in result:
                    result.append(item)
    return result


def plan(policy, metadata, decisions, year):
    if policy['division'] != transport.DIVISION or policy['rsin'] != tax.RSIN:
        raise transport.Stop('Wrong tax rule scope')
    account = transport.guid(metadata['tax_account_id'])
    verified = {}
    for bucket, code in policy['gl_accounts'].items():
        match = [a for a in metadata['accounts'] if str(a.get('Code') or '').strip() == code
                 and a.get('BalanceType') == 'B' and a.get('IsBlocked') is False]
        if len(match) != 1:
            raise transport.Stop('Tax balance account unavailable')
        verified[bucket] = transport.guid(match[0]['ID'])
    result = {}
    for template in templates(policy, decisions):
        letter = template['letter']
        periods = (template['period'],) if letter == 'V' else PERIODS[template['frequency']]
        for target_year in (year - 1, year, year + 1):
            for period in periods:
                for kind in (range(6) if letter == 'V' else (None,)):
                    d = reference(letter, target_year, period, subnumber=template.get('subnumber'), kind=kind)
                    payload = {'Account': account, 'GLAccount': verified[d['tax_bucket']], 'Words': d['payment_reference']}
                    result[payload['Words']] = {'payload': payload, 'tax_bucket': d['tax_bucket'],
                                               'tax_year': target_year, 'assessment_number': d['assessment_number']}
    for d in decisions:
        if (d.get('status') == 'TAX_IDENTIFIED' and d.get('reference_source') == 'explicit_refund_description'
                and d.get('direction') == 'incoming' and d.get('tax_bucket') == 'btw'):
            # Revalidate stored observations after upgrades, not just their status.
            checked = tax.vat_refund_decision({'Description': d.get('allocation_words'),
                'Account': metadata['tax_account_id'], 'AmountDC': d.get('amount_dc'),
                'Date': f"{d['tax_year']}-12-31"})
            if checked:
                payload = {'GLAccount': verified['btw'], 'Words': checked['allocation_words']}
                result[payload['Words']] = {'payload': payload, 'tax_bucket': 'btw',
                    'tax_year': d['tax_year'], 'assessment_number': d.get('assessment_number')}
    return result


def match_rule(rules, payload):
    matches = []
    for rule in rules:
        words = str(rule.get('Words') or '').strip()
        if (payload.get('Account') and str(rule.get('Account') or '').lower() == payload['Account'].lower()
                and rule.get('GLAccount') and (not words or 'belastingdienst' in words.lower())):
            return 'conflict', None
        # Detect a matching full reference even when an older rule uses spaces.
        wanted = payload['Words']
        reference_match = wanted in re.sub(r'[ .\t\u00a0-]', '', words) if wanted.isdigit() else words == wanted
        if not reference_match:
            continue
        if words != wanted or any(str(rule.get(k) or '').lower() != str(payload.get(k) or '').lower() for k in ('Account', 'GLAccount')) or any(rule.get(k) for k in ('AccountBankAccount', 'Costcenter', 'Costunit', 'VATCode')):
            return 'conflict', None
        matches.append(rule)
    if len(matches) > 1:
        return 'conflict', None
    return ('confirmed', transport.guid(matches[0]['ID'])) if matches else ('missing', None)


class RuleAPI(CollectionAPI):
    def __init__(self, app, allowed, limits):
        super().__init__(app, role='tax', priority='routine', floor=150)
        self.allowed = allowed
        self.limits = dict(limits)

    async def request(self, method, url, params=None, payload=None):
        parsed = urlparse(url)
        valid = parsed.scheme == 'https' and parsed.netloc == 'start.exactonline.nl' and parsed.path == urlparse(ROOT).path and not parsed.fragment and not parsed.username
        if not valid or method not in ('GET', 'POST') or (method == 'GET' and payload is not None):
            raise transport.Stop('Tax rules endpoint only')
        if method == 'POST' and (url != ROOT or not isinstance(payload, dict) or payload != self.allowed.get(payload.get('Words'))):
            raise transport.Stop('Unapproved tax rule payload')
        if self.limits.get('remaining', 1000) <= 150:
            raise transport.Stop('Tax rules API reserve')
        await asyncio.sleep(max(0, 1.2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        token = await self.app._access_token()
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False, verify=transport.TLS_CONTEXT) as client:
            from operations.worker_coordination import budgeted_http
            response = await budgeted_http(self.app, self.role, method,
                lambda: client.request(method, url, params=params, json=payload,
                    headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}),
                priority=self.priority, floor=self.floor)
        self.limits = {name: int(response.headers[header]) for name, header in (
            ('remaining', 'x-ratelimit-remaining'), ('reset_ms', 'x-ratelimit-reset'))
            if response.headers.get(header, '').isdigit()}
        if response.status_code not in ((200,) if method == 'GET' else (200, 201, 204)):
            raise transport.ExactRequestError(method, response.status_code, self.limits)
        return response.json() if response.content else {}


def initialize(conn):
    writes.initialize(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS jnp_tax_rules (
        words TEXT PRIMARY KEY, payload JSONB NOT NULL, details JSONB NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', rule_id UUID, reason TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


def save_state(conn, words, state, rule_id=None, reason=None):
    conn.execute('UPDATE jnp_tax_rules SET state=%s,rule_id=%s,reason=%s,updated_at=NOW() WHERE words=%s',
                 (state, rule_id, reason, words))


async def reconcile(conn, api, proposals):
    rules = await api.rules()
    attempted = []
    result_state = 'ready'
    for words, item in proposals.items():
        if task_drain.requested():
            result_state = 'draining'
            break
        payload = item['payload']
        conn.execute('''INSERT INTO jnp_tax_rules(words,payload,details) VALUES(%s,%s::jsonb,%s::jsonb)
            ON CONFLICT(words) DO NOTHING''', (words, json.dumps(payload), json.dumps(item)))
        previous, saved_payload = conn.execute('SELECT state,payload FROM jnp_tax_rules WHERE words=%s', (words,)).fetchone()
        if saved_payload != payload:
            save_state(conn, words, 'conflict', reason='Stored target differs'); continue
        state, rule_id = match_rule(rules, payload)
        if state != 'missing':
            save_state(conn, words, state, rule_id); continue
        if previous in ('creating', 'uncertain', 'confirmed', 'conflict'):
            save_state(conn, words, 'uncertain', reason='No blind recreation'); continue
        if len(attempted) >= 40 or api.limits.get('remaining', 1000) <= 155:
            continue
        attempted.append(words)
        try:
            # Each owned operation persists its own readback before another POST.
            rules = await writes.create_confirmed_rule(conn, api, payload)
        except writes.CreationDeferred:
            result_state = 'waiting_for_api_budget'
            break
        except Exception:
            result_state = 'review_required'
            break
    counts = dict(conn.execute('SELECT state,COUNT(*) FROM jnp_tax_rules GROUP BY state').fetchall())
    return {'state': result_state, 'counts': counts, 'planned': len(proposals),
            'by_tax': dict(Counter(item['tax_bucket'] for item in proposals.values())),
            'bank_writes': False, 'automatically_executed': False,
            'last_check': datetime.now(timezone.utc).isoformat()}


async def sync(app, conn, policy, metadata, limits):
    if not policy.get('allocation_rule_writes'):
        STATUS['state'] = 'disabled'; return
    initialize(conn)
    decisions = [row[0] for row in conn.execute('SELECT decision FROM jnp_tax_observations').fetchall()]
    proposals = plan(policy, metadata, decisions, datetime.now(timezone.utc).year)
    api = RuleAPI(allocation.RoutingApp(app), {w: p['payload'] for w, p in proposals.items()}, limits)
    STATUS.update(await reconcile(conn, api, proposals))
    if task_drain.requested() or STATUS['state'] != 'ready':
        return
    # Retire only the verified legacy IBAN->creditor fallback, with a saved
    # payload and a fresh equality check. Unknown conflicting rules are reported.
    from operations import allocation_maintenance as maintenance
    cleanup_api = maintenance.MaintenanceAPI(allocation.RoutingApp(app), api.limits)
    cleanup_api.role = 'tax'
    maintenance.initialize_cleanup(conn)
    rules = await cleanup_api.rules()
    retired = {}
    for rule in rules:
        if task_drain.requested():
            STATUS['state'] = 'draining'
            return
        if legacy_creditor_fallback(rule, metadata['tax_account_id']):
            if all(match_rule(rules, p['payload'])[0] == 'confirmed' for p in proposals.values()):
                retired[rule['ID']] = await writes.retire_confirmed_rule(conn, cleanup_api, rule, metadata['tax_account_id'])
    current = await cleanup_api.rules() if retired else rules
    conflicts = audit_rules(current, metadata, decisions)
    STATUS.update(legacy_rules_retired=retired, rule_conflicts=conflicts,
                  tax_counterparty_fallback='review_only')


def legacy_creditor_fallback(rule, tax_account_id):
    return (str(rule.get('ID') or '').lower() == '2a9fa4f3-56eb-462b-843a-919b4fed9e83'
            and str(rule.get('Account') or '').lower() == tax_account_id.lower()
            and rule.get('AccountBankAccount') == 'NL04RABO0200112244'
            and not any(rule.get(k) for k in ('Words', 'GLAccount', 'Costcenter', 'Costunit', 'VATCode')))


def audit_rules(rules, metadata, decisions):
    """Report overlaps without claiming Exact's undocumented rule precedence."""
    findings = []
    tax_id = metadata['tax_account_id'].lower()
    for rule in rules:
        account = str(rule.get('Account') or '').lower()
        words = str(rule.get('Words') or '').strip()
        iban = re.sub(r'\s+', '', str(rule.get('AccountBankAccount') or '')).upper()
        if (account == tax_id or iban in tax.TAX_IBANS) and not rule.get('GLAccount'):
            findings.append({'rule_id': rule['ID'], 'reason': 'tax_rule_without_ledger'})
        elif words and account and account != tax_id and any(
                words.lower() in str(d.get('allocation_words') or '').lower() for d in decisions):
            findings.append({'rule_id': rule['ID'], 'reason': 'other_relation_matches_tax_description'})
    return findings
