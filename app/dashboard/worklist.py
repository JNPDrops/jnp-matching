"""Private projections of existing agent dossiers. No Exact/PSP calls or writes.

Legacy sources belong exclusively to 3977752. Human decisions are separate,
audited workflow records; nothing here marks an accounting operation executed.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
import re

import psycopg

DIVISION = "3977752"
LIMIT = 10000
SOURCE_NAMES = {"bank": "Bankbetalingen", "fibonatix": "Fibonatix-orderkoppelingen",
                "routing": "Debiteurenroutering", "icepay": "ICEPAY-import"}
CHOICES = {
    "request_invoice": "Factuur of creditnota opvragen",
    "await_import": "Wachten op order- of factuurimport",
    "inspect_payment": "Betaalbewijs en orderkoppeling onderzoeken",
    "inspect_existing_match": "Bestaande aflettering laten controleren",
    "review_difference": "Betalingsverschil afzonderlijk laten beoordelen",
    "propose_own_invoice": "Aflettering tegen de eigen factuur voorstellen",
    "review_duplicate": "Mogelijke dubbele import onderzoeken",
    "provide_evidence": "Onderbouwing aangevuld; opnieuw laten beoordelen",
}
LABELS = {
    "invoice_missing": "Verkoopfactuur nog niet gevonden",
    "invoice_not_open": "Bestaande aflettering controleren",
    "supplier_invoice_missing": "Inkoopfactuur ontbreekt",
    "supplier_review": "Factuur of bon nodig",
    "receipt_identity_needed": "Herkomst ontvangst onbekend",
    "suspense_identity_needed": "Vraagpost: bronbewijs nodig",
    "duplicate_review": "Mogelijke dubbele betaling",
    "amount_review": "Betalingsverschil beoordelen",
    "order_amount_review": "Orderbedrag wijkt af",
    "payment_method_review": "Betaalroute controleren",
    "account_review": "Relatie controleren",
    "multiple_invoices": "Meerdere mogelijke facturen",
    "multiple_payments_review": "Meerdere betalingen voor één order",
    "refund_review": "Terugbetaling controleren",
    "order_refund_review": "Refund of creditnota controleren",
    "psp_deferred": "PSP-afrekening nodig",
    "tax_review": "Belastingonderbouwing nodig",
    "bacs_match_candidate": "Bankbetaling: matchvoorstel controleren",
    "supplier_match_candidate": "Leveranciersbetaling: matchvoorstel controleren",
    "psp_match_candidate": "PSP-betaling: eigen factuur koppelen",
    "invoice_missing_or_ambiguous": "Eigen factuur ontbreekt of is niet uniek",
    "invoice_other_debtor": "Eigen factuur staat op andere debiteur",
    "amount_difference_requires_case_decision": "Betalingsverschil vraagt een besluit",
    "multiple_receipts_for_source_order": "Meerdere ontvangsten voor één bronorder",
    "own_invoice_not_fully_open": "Eigen factuur is niet volledig open",
}


def string(value, limit=2000):
    return str(value)[:limit] if value is not None else None


def timestamp(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return string(value, 80)


def epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (ValueError, TypeError):
        return 0


def money(value):
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        return format(result, "f") if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def identifier(source, key):
    return hashlib.sha256((DIVISION + ":" + source + ":" + str(key)).encode()).hexdigest()[:32]


def provider(item):
    method = item.get("payment_method")
    mapped = {"wc_fibonatics": "Fibonatix", "plisio": "Plisio", "bacs": "Bank",
              "ic": "ICEPAY", "icepay": "ICEPAY", "np_payments": "NinjaPay",
              "suap_wordpresspayplugin": "SUAP"}
    if method in mapped:
        return mapped[method]
    journal = str(item.get("journal_code") or "").strip()
    if journal in {"26", "27"}:
        return {"26": "Fibonatix", "27": "ICEPAY"}[journal]
    return string(item.get("provider") or item.get("journal") or "Bank", 100)


def choices(category):
    values = ["inspect_payment", "provide_evidence"]
    if "invoice" in category or "supplier" in category or "factuur" in category:
        values = ["request_invoice", "await_import", "inspect_existing_match"] + values
    if "amount" in category or "difference" in category:
        values.insert(0, "review_difference")
    if "duplicate" in category:
        values.insert(0, "review_duplicate")
    if "candidate" in category or category == "own_invoice_match":
        values.insert(0, "propose_own_invoice")
    return [{"value": v, "label": CHOICES[v]} for v in dict.fromkeys(values)]


def case(source, key, category, observed_at, **fields):
    item = dict(id=identifier(source, key), source=source, division=DIVISION,
                category=category, title=LABELS.get(category, "Beoordeling nodig"),
                status="open", observed_at=timestamp(observed_at),
                execution_status="not_confirmed", execution_label="Afhandeling niet bevestigd",
                options=choices(category), evidence=[], aliases=[], **fields)
    return item


def bank_case(key, raw, observed, source="bank"):
    # Refuse explicitly foreign records even though the legacy table is single-division.
    if str(raw.get("division", DIVISION)) != DIVISION:
        return None
    category = str(raw.get("status") or "bank_identity_review")
    action = string(raw.get("next_action")) or "Controleer de bronbetaling en de specifieke bijbehorende factuur."
    if "automatically" in action.lower():
        action = "Controleer de specifieke tegenboeking en pas uitsluitend de onderbouwde toewijzing toe; bevestig daarna het resultaat."
    item = case(source, key, category, raw.get("observed_at") or observed,
        psp=provider(raw), reference=string(raw.get("reference"), 120),
        transaction_id=string(raw.get("payment_transaction_id"), 150),
        woo_order=string(raw.get("woo_order_id"), 100),
        entry_id=string(raw.get("bank_entry_id"), 80), entry_number=string(raw.get("entry_number"), 60),
        debtor=string(raw.get("account_code"), 60), journal=string(raw.get("journal_code"), 40),
        currency=string(raw.get("currency"), 10), amount=money(raw.get("original_bank_amount_signed", raw.get("amount"))),
        remaining_amount=money(raw.get("remaining_bank_amount_signed")),
        invoice_amount=money(raw.get("invoice_amount")), invoice_remaining=money(raw.get("invoice_open_amount_signed")),
        difference=money(raw.get("difference")), invoice_number=string(raw.get("invoice_entry"), 80),
        invoice_reference=string(raw.get("invoice_reference"), 120),
        description=string(raw.get("description")), reason=string(raw.get("reason")) or "Onderbouwing ontbreekt.",
        next_action=action, needed_information=string(raw.get("needed_information")) or "Bronbetaling, order en bijbehorende Exact-factuur.",
        retry_when=string(raw.get("retry_when")), suggested_owner=string(raw.get("action_owner") or "Boekhouding", 120))
    item["title"] = string(raw.get("status_label"), 160) or item["title"]
    item["status"] = "waiting" if raw.get("work_group") == "waiting" else "open"
    item["aliases"] = [str(raw[k]).lower() for k in ("bank_line_id", "cashflow_id") if raw.get(k)]
    for field, label in (("open_evidence", "Bewijs openstaand"), ("order_status", "Orderstatus"),
                         ("payment_method", "Betaalmethode"), ("order_check", "Ordercontrole")):
        if raw.get(field) is not None:
            item["evidence"].append({"label": label, "value": string(raw[field], 500)})
    for entry in (raw.get("existing_sales_entries") or [])[:20]:
        item["evidence"].append({"label": "Bestaande verkoopboeking", "value": string(entry.get("EntryNumber"), 80)})
    if raw.get("possible_duplicate_ids"):
        item["evidence"].append({"label": "Mogelijke dubbele bankregels", "value": ", ".join(map(str, raw["possible_duplicate_ids"][:20]))})
    return item


def strict_cases(job, plan, updated):
    result = []
    final = plan.get("final_readback") or {}
    for receipt in plan.get("receipts", []):
        state = receipt.get("state")
        reference = receipt.get("source_order")
        category = receipt.get("exception") or "own_invoice_match"
        events = receipt.get("evidence") or []
        observed = max([plan.get("created_at"), final.get("at")] + [e.get("at") for e in events], key=epoch)
        invoice = receipt.get("invoice") or {}
        inv_matches = [r for r in final.get("receivables", []) if r.get("YourRef") == reference and str(r.get("JournalCode")) == "70" and str(r.get("InvoiceNumber")) == str(invoice.get("EntryNumber"))]
        open_matches = [r for r in final.get("receivables", []) if str(r.get("JournalCode")) == "26" and receipt.get("trx") and receipt["trx"] in str(r.get("Description") or "")]
        amount, inv_amount = money(receipt.get("amount")), money(invoice.get("AmountDC"))
        reason = LABELS.get(category, "De ontvangst moet aantoonbaar bij de eigen bronorder en factuur terechtkomen.")
        if receipt.get("allocated_reference") not in (None, "", reference):
            reason += " De oorspronkelijke toewijzingsreferentie wijkt af; dit is op zichzelf geen bewijs van een verkeerde aflettering."
        item = case("fibonatix", job + ":" + str(receipt.get("bank_line_id") or receipt.get("trx")), category, observed or updated,
            psp="Fibonatix", reference=string(reference, 120), woo_order=string(receipt.get("woo"), 100),
            transaction_id=string(receipt.get("trx"), 150), entry_id=string(receipt.get("entry_id"), 80),
            entry_number=string(receipt.get("entry_number"), 60), debtor="100100", journal="26", currency="EUR",
            amount=amount, invoice_amount=inv_amount,
            remaining_amount=money(open_matches[0].get("Amount")) if len(open_matches) == 1 else None,
            invoice_remaining=money(inv_matches[0].get("Amount")) if len(inv_matches) == 1 else None,
            difference=money(Decimal(amount)-Decimal(inv_amount)) if amount is not None and inv_amount is not None else None,
            invoice_number=string(invoice.get("EntryNumber"), 80), invoice_reference=string(invoice.get("YourRef"), 120),
            description=string(receipt.get("description")), reason=reason,
            next_action="Controleer de oorspronkelijke PSP-betaling, de eigen order en de werkelijk gekoppelde factuur. Een gelijk bedrag alleen is onvoldoende.",
            needed_information="Eigen factuur, bestaande afletteringen en deelbetalingen; leg een eventueel betalingsverschil per order voor.",
            retry_when="Na aanvulling van het order- en factuurbewijs", suggested_owner="Boekhouding")
        item["aliases"] = [str(receipt[k]).lower() for k in ("bank_line_id", "offset_id") if receipt.get(k)]
        if receipt.get("offset_id"):
            item["id"] = identifier("bank", str(receipt["offset_id"]).lower())
        item["evidence"] = [{"label": "Agentstatus", "value": string(state)},
                            {"label": "Oorspronkelijke toewijzingsreferentie", "value": string(receipt.get("allocated_reference"))}]
        # Only actual inspected Exact selections can be labelled as matched identities.
        for row in (events[-1].get("rows", []) if events else []):
            cells = row.get("cells", [])
            if row.get("checked") and len(cells) > 4:
                item["evidence"].append({"label": "In Exact geselecteerde factuur / referentie", "value": string(str(cells[2]) + " / " + str(cells[4]), 200)})
        if state == "matched_verified":
            item.update(status="resolved", execution_status="verified", execution_label="Eigen orderkoppeling door agent teruggelezen", title="Eigen orderkoppeling bevestigd")
        elif state in {"undo_requested", "match_requested", "correct_requested"}:
            item.update(title="Uitkomst van eerdere verwerking controleren", execution_status="uncertain", execution_label="Uitkomst onzeker; niet opnieuw uitvoeren")
        elif state == "exception_verified":
            item["execution_label"] = "Open ontvangst door agent bevestigd"
        result.append(item)
    return result


def routing_case(row, observed):
    entry_id, reference, state, reason, order, order_ref, next_check = row
    order = order or {}
    item = case("routing", entry_id, "routing_" + state, observed,
        psp=provider(order), reference=string(order_ref or reference, 120), transaction_id=None,
        woo_order=string(order.get("order_id"), 100), entry_id=str(entry_id), entry_number=None,
        debtor=None, journal=None, currency=string(order.get("currency"), 10), amount=None,
        remaining_amount=None, invoice_amount=None, invoice_remaining=None, difference=None,
        invoice_number=None, invoice_reference=string(reference, 120), description=None,
        reason="De routeringsagent heeft deze boeking nog niet bevestigd.",
        next_action="Controleer betaalmethode, bronorder en huidige debiteur. Controleer een onzekere eerdere wijziging eerst in Exact.",
        needed_information="Orderbewijs en de bestaande Exact-verkoopboeking; maak geen nieuwe debiteur aan.",
        retry_when=timestamp(next_check), suggested_owner="Boekhouding")
    item["title"] = "Debiteurwijziging controleren" if state != "pending" else "Wachten op orderbewijs"
    item["status"] = "waiting" if state == "pending" else "open"
    item["execution_status"] = "uncertain" if state == "uncertain" else "not_confirmed"
    # Reasons may originate in exceptions. Expose only known, nonsecret classes.
    if reason == "Order not yet present in Metorik; no inference":
        item["reason"] = "De bijbehorende order is nog niet in Metorik gevonden; de agent probeert opnieuw."
    item["evidence"] = [{"label": "Agentstatus", "value": state}, {"label": "Betaalmethode", "value": string(order.get("payment_method"), 100)}]
    return item


def finalize(items):
    # Same exact bank-line identity, never just an equal amount/order number.
    latest = {}
    for item in sorted(items, key=lambda x: epoch(x.get("observed_at")), reverse=True):
        aliases = set(item["aliases"])
        if aliases and any(a in latest for a in aliases):
            continue
        for alias in aliases:
            latest[alias] = item["id"]
        item["stale"] = not epoch(item.get("observed_at")) or datetime.now(timezone.utc).timestamp() - epoch(item["observed_at"]) > 7200
        material = {k:v for k,v in item.items() if k not in {"observed_at", "stale", "aliases"}}
        item["fingerprint"] = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
        item["revision"] = 0
        yield item


class Conflict(Exception):
    pass


class PostgresWorklist:
    def __init__(self, database_url):
        self.database_url = database_url

    def connect(self):
        conn = psycopg.connect(self.database_url, connect_timeout=5)
        conn.execute("SET statement_timeout = '8s'")
        conn.commit()
        return conn

    @staticmethod
    def exists(conn, name):
        return conn.execute("SELECT to_regclass(%s)", ("public." + name,)).fetchone()[0] is not None

    def read_source(self, source):
        items, observed = [], None
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            if source == "bank":
                if not all(self.exists(conn, name) for name in ("jnp_suspense_review", "jnp_bank_identity_review", "jnp_allocation_maintenance")):
                    return [], {"status": "unavailable"}
                row = conn.execute("SELECT summary FROM jnp_allocation_maintenance WHERE singleton=TRUE").fetchone()
                observed = (row[0] if row else {}).get("last_scan")
                for table, key in (("jnp_suspense_review", "bank_line_id"), ("jnp_bank_identity_review", "cashflow_id")):
                    rows = conn.execute(f"SELECT {key}::text,details,observed_at FROM {table} ORDER BY {key} LIMIT %s", (LIMIT+1,)).fetchall()
                    if len(rows) > LIMIT:
                        raise ValueError("source_limit_exceeded")
                    for key_value, raw, at in rows:
                        item = bank_case(key_value, raw, at)
                        if item:
                            items.append(item)
                if items:
                    observed = max([observed] + [i["observed_at"] for i in items], key=epoch)
                return items, {"status": "ready" if observed else "not_scanned", "observed_at": timestamp(observed)}
            if source == "fibonatix":
                if not self.exists(conn, "fibonatix_import_artifacts"):
                    return [], {"status": "unavailable"}
                rows = conn.execute("""SELECT a.job,a.data,j.updated_at FROM fibonatix_import_artifacts a
                    JOIN fibonatix_import_jobs j ON j.job=a.job WHERE a.name='strict_order_plan' ORDER BY j.updated_at DESC LIMIT 21""").fetchall()
                if len(rows) > 20:
                    raise ValueError("source_limit_exceeded")
                for job, data, updated in rows:
                    if len(data.get("receipts", [])) > LIMIT:
                        raise ValueError("source_limit_exceeded")
                    items += strict_cases(job, data, updated)
                observed = max((i["observed_at"] for i in items), key=epoch, default=None)
                return items, {"status": "ready" if rows else "not_scanned", "observed_at": observed}
            if source == "routing":
                if not self.exists(conn, "jnp_debtor_route_queue"):
                    return [], {"status": "unavailable"}
                control = conn.execute("SELECT last_scan FROM jnp_debtor_route_control WHERE singleton=TRUE").fetchone()
                observed = timestamp(control[0]) if control else None
                rows = conn.execute("""SELECT entry_id::text,reference,state,reason,order_evidence,order_reference,next_check
                    FROM jnp_debtor_route_queue WHERE state IN ('uncertain','blocked','failed','error','review')
                    OR (state='pending' AND reason='Order not yet present in Metorik; no inference')
                    ORDER BY entry_id LIMIT %s""", (LIMIT+1,)).fetchall()
                if len(rows) > LIMIT:
                    raise ValueError("source_limit_exceeded")
                return [routing_case(r, observed) for r in rows], {"status": "ready" if observed else "not_scanned", "observed_at": observed}
            if source == "icepay":
                if not self.exists(conn, "icepay_receipt_import_runs"):
                    return [], {"status": "unavailable"}
                row = conn.execute("""SELECT task,created_at,summary FROM icepay_receipt_import_runs
                    ORDER BY created_at DESC LIMIT 1""").fetchone()
                if not row:
                    return [], {"status": "not_scanned"}
                task, at, summary = row
                state = summary.get("state")
                if state in {"blocked", "requires_review", "requires_review_no_retry", "prior_write_claim_reconcile_only", "upload_requested", "reading_back"}:
                    item = case("icepay", task, "icepay_import_review", at, psp="ICEPAY", reference=task,
                        currency="EUR", amount=None, remaining_amount=None, debtor="109419", journal="27",
                        reason="De import is nog niet volledig door teruglezing bevestigd.",
                        next_action="Vergelijk de unieke PaymentID’s met de Exact-bankregels. Herhaal een eerdere upload niet zonder vastgesteld resultaat.",
                        needed_information="PSP-bronbestand, importresultaat en Exact-bankregels per PaymentID.")
                    item.update(title="ICEPAY-importresultaat controleren", execution_status="uncertain",
                        execution_label="Resultaat eerst teruglezen", evidence=[{"label":"Agentstatus", "value":string(state)},
                        {"label":"Bevestigde ontvangsten", "value":string(summary.get("verified_receipts", 0))}])
                    items.append(item)
                return items, {"status": "ready", "observed_at": timestamp(at), "agent_state": string(state, 100)}
        raise ValueError("unknown_source")

    def workflow(self, division):
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            if not self.exists(conn, "dashboard_worklist_cases"):
                return {}
            rows = conn.execute("SELECT case_id,state,revision FROM dashboard_worklist_cases WHERE division=%s ORDER BY updated_at DESC LIMIT %s", (division, LIMIT+1)).fetchall()
            if len(rows) > LIMIT:
                raise ValueError("workflow_limit_exceeded")
            return {key: (state, revision) for key, state, revision in rows}

    def read(self, division):
        if division != DIVISION or str(os.getenv("EXACT_DIVISION", DIVISION)) != division:
            return {"division": division, "connected": False, "complete": False, "items": [], "sources": [], "reason": "Administratiebron nog niet aangesloten"}
        items, sources = [], []
        for source, name in SOURCE_NAMES.items():
            try:
                projected, status = self.read_source(source)
                items += projected
                status["count"] = sum(i["status"] != "resolved" for i in projected)
            except Exception as error:
                status = {"status": "error", "count": None, "error_type":type(error).__name__}
            status.update(id=source, name=name)
            status["stale"] = not epoch(status.get("observed_at")) or datetime.now(timezone.utc).timestamp() - epoch(status.get("observed_at")) > 7200
            sources.append(status)
        items = list(finalize(items))
        try:
            saved = self.workflow(division)
            workflow_ready = True
        except Exception:
            saved, workflow_ready = {}, False
        known = {i["id"] for i in items}
        for item in items:
            state, revision = saved.get(item["id"], ({}, 0))
            item["revision"] = revision
            item["workflow"] = {k:state[k] for k in ("assigned_to", "assigned_name", "decision", "note", "actor_name", "updated_at", "history") if k in state}
            if item["status"] != "resolved" and state:
                item["source_changed"] = state.get("fingerprint") != item["fingerprint"]
                if not item["source_changed"]:
                    item["status"] = state.get("status", item["status"])
            item["editable"] = item["status"] != "resolved" and workflow_ready
        for key, (state, revision) in saved.items():
            if key not in known and state.get("snapshot"):
                item = dict(state["snapshot"])
                item.update(id=key, revision=revision, status="waiting", editable=False, source_absent=True,
                    execution_status="not_confirmed", execution_label="Niet in actuele bronlijst; afhandeling niet bewezen",
                    workflow={k:state[k] for k in ("assigned_to", "assigned_name", "decision", "note", "actor_name", "updated_at", "history") if k in state})
                items.append(item)
        items.sort(key=lambda i: (i["status"] == "resolved", i.get("stale", True), i.get("psp", ""), i.get("reference") or i["id"]))
        return {"division": division, "connected": any(s["status"] == "ready" for s in sources),
                "complete": all(s["status"] == "ready" for s in sources), "sources": sources, "items": items,
                "workflow_ready": workflow_ready, "read_at": datetime.now(timezone.utc).isoformat(),
                "financial_execution_enabled": False}

    def change(self, division, case_id, actor, action, note, decision, revision, fingerprint):
        current = self.read(division)
        item = next((i for i in current["items"] if i["id"] == case_id), None)
        if not item or not item.get("editable") or item["fingerprint"] != fingerprint:
            raise Conflict("De bron of vraag is gewijzigd. Ververs de werklijst.")
        if action not in {"claim", "wait", "reopen", "decide", "note"} or len(note) > 4000:
            raise ValueError("Ongeldig verzoek")
        if action in {"wait", "decide", "note"} and not note.strip():
            raise ValueError("Een toelichting is verplicht")
        if action == "decide" and decision not in {o["value"] for o in item["options"]}:
            raise ValueError("Kies een beschikbare vervolgstap")
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('dashboard_worklist_schema',0))")
            conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_worklist_cases (
                division TEXT NOT NULL,case_id TEXT NOT NULL,state JSONB NOT NULL,revision INTEGER NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(division,case_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_worklist_events (
                id BIGSERIAL PRIMARY KEY,division TEXT NOT NULL,case_id TEXT NOT NULL,
                actor_oid TEXT NOT NULL,event JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (division+":"+case_id,))
            row = conn.execute("SELECT state,revision FROM dashboard_worklist_cases WHERE division=%s AND case_id=%s FOR UPDATE", (division, case_id)).fetchone()
            previous, current_revision = row if row else ({}, 0)
            if revision != current_revision:
                raise Conflict("Een collega heeft deze vraag gewijzigd. Ververs de werklijst.")
            at = datetime.now(timezone.utc).isoformat()
            event = dict(action=action, note=note.strip(), decision=decision if action == "decide" else None,
                         actor_name=string(actor["name"], 200), actor_oid=actor["oid"], at=at,
                         source_fingerprint=fingerprint, execution_status="not_started")
            state = {**previous, "status": item["status"], "fingerprint":fingerprint, "updated_at":at,
                     "actor_name":event["actor_name"], "snapshot":{k:v for k,v in item.items() if k not in {"workflow", "revision"}},
                     "history":(previous.get("history", []) + [event])[-50:]}
            if previous.get("fingerprint") != fingerprint:
                state.pop("decision", None)
            if action == "claim":
                state.update(assigned_to=actor["oid"], assigned_name=event["actor_name"])
            elif action in {"wait", "reopen", "decide"}:
                state["status"] = {"wait":"waiting", "reopen":"open", "decide":"decided"}[action]
                state["decision"] = decision if action == "decide" else None
            if note.strip():
                state["note"] = note.strip()
            conn.execute("""INSERT INTO dashboard_worklist_cases(division,case_id,state,revision) VALUES(%s,%s,%s::jsonb,%s)
                ON CONFLICT(division,case_id) DO UPDATE SET state=EXCLUDED.state,revision=EXCLUDED.revision,updated_at=NOW()""",
                (division,case_id,json.dumps(state),revision+1))
            conn.execute("INSERT INTO dashboard_worklist_events(division,case_id,actor_oid,event) VALUES(%s,%s,%s,%s::jsonb)",
                (division,case_id,actor["oid"],json.dumps({**event, "source_snapshot":state["snapshot"]})))
        return {"saved":True, "revision":revision+1, "financial_execution_started":False}


async def readiness_probe():
    """One internal, read-only startup check; aggregate status only in logs."""
    if os.getenv("DASHBOARD_ENABLED", "false").lower() != "true" or not os.getenv("DATABASE_URL"):
        return
    result = await asyncio.to_thread(PostgresWorklist(os.environ["DATABASE_URL"]).read, DIVISION)
    logging.getLogger("uvicorn.error").info("dashboard_worklist_ready %s", json.dumps({
        "connected":result["connected"], "complete":result["complete"],
        "sources":result["sources"], "case_count":len(result["items"]),
        "status_counts":{s:sum(i["status"]==s for i in result["items"]) for s in ("open","waiting","decided","resolved")},
        "financial_execution_enabled":False}))
