"""Explicit-manifest, read-only order evidence for the one-shot bacs correction.

No webhook, background task, matching, payment-condition write or other route.
Metorik docs: https://metorik.dev/#orders (order_number in filter).
"""
import asyncio
from datetime import datetime, timezone
import json
import os
import re
from zoneinfo import ZoneInfo

import httpx

from operations import bacs_debtor_transfer as m

BASE = "https://app.metorik.com/api/v1/store"
FIELDS = ("order_id", "order_number", "payment_method", "currency", "total",
          "total_refunds", "status", "order_created_at", "order_updated_at")


def manifest_rows(manifest):
    m.require(isinstance(manifest, list) and 1 <= len(manifest) <= 100,
              "Expected 1-100 explicitly approved entry/order mappings")
    result = []
    for row in manifest:
        m.require(isinstance(row, dict) and set(row) == {"entry_id", "reference", "order_id"},
                  "Unexpected manifest fields")
        m.require(isinstance(row["reference"], str) and re.fullmatch(r"TD[0-9]{4,10}", row["reference"]),
                  "Invalid manifest webshop reference")
        m.require(type(row["order_id"]) is int and row["order_id"] > 0, "Invalid source order ID")
        result.append({**row, "entry_id": m.guid(row["entry_id"])})
    for key in ("entry_id", "reference", "order_id"):
        m.require(len({r[key] for r in result}) == len(result), "Ambiguous/duplicate manifest mapping")
    return sorted(result, key=lambda r: r["reference"])


async def lookup_orders(references, include_line_proof=False):
    m.require(isinstance(references, list) and 1 <= len(references) <= 100
              and len(set(references)) == len(references)
              and all(isinstance(r, str) and re.fullmatch(r"TD[0-9]{4,10}", r) for r in references),
              "Expected unique webshop references")
    key = os.environ.get("METORIK_API_KEY", "").strip()
    m.require(bool(key), "Metorik authentication unavailable")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False,
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"}) as client:
        async def get(path, params=None):
            m.require(path in ("", "/orders"), "Metorik read path not allowed")
            await asyncio.sleep(1.2)
            try:
                response = await client.get(BASE + path, params=params)
            except httpx.HTTPError:
                raise m.Stop("Metorik read transport failure; details suppressed") from None
            m.require(response.status_code == 200, f"Metorik GET HTTP {response.status_code}; body suppressed")
            try:
                body = response.json()
            except ValueError:
                raise m.Stop("Invalid Metorik JSON") from None
            m.require(isinstance(body, dict), "Invalid Metorik response")
            return body
        store = await get("")
        store = {k: store.get(k) for k in ("name", "timezone", "currency", "platform")}
        m.require(store == {"name": "TheDrops.eu", "timezone": "Europe/Amsterdam",
                            "currency": "EUR", "platform": "woocommerce"}, "Unexpected Metorik store")
        wanted = {"#" + r[2:] for r in references}
        orders = {}
        numbers = sorted(wanted)
        # Live Metorik validation limits an in-filter to 25 values.
        for offset in range(0, len(numbers), 25):
            batch_numbers = numbers[offset:offset + 25]
            for page in range(1, 11):
                body = await get("/orders", {"page": page, "per_page": 100,
                    "filters": json.dumps([{"field": "order_number", "operator": "in", "value": [n[1:] for n in batch_numbers]}])})
                rows, pg = body.get("data"), body.get("pagination")
                m.require(isinstance(rows, list) and isinstance(pg, dict)
                    and pg.get("current_page") == page and pg.get("per_page") == 100
                    and type(pg.get("has_more_pages")) is bool and len(rows) <= 100,
                    "Invalid Metorik pagination")
                for raw in rows:
                    # Do not store personal customer details returned by the API.
                    row = {k: raw.get(k) for k in FIELDS}
                    if include_line_proof:
                        row["line_proof"] = {"items": [{k: i.get(k) for k in ("line_item_id", "sku", "quantity", "total", "total_tax")} for i in raw.get("line_items", [])],
                            "total_discount": raw.get("total_discount"), "shipping_method_title": raw.get("shipping_method_title")}
                    number = row["order_number"]
                    m.require(number in batch_numbers and number not in orders
                              and type(row["order_id"]) is int and row["order_id"] > 0,
                              "Metorik order mapping missing, changed or ambiguous")
                    orders[number] = row
                if not pg["has_more_pages"]:
                    break
                m.require(bool(rows), "Empty intermediate Metorik page")
            else:
                raise m.Stop("Incomplete Metorik pagination")
    return {"store": store, "orders": orders}


async def read_orders(manifest, reviewed_line_links=None):
    manifest = manifest_rows(manifest)
    links = sorted(reviewed_line_links or [])
    m.require(len(set(links)) == len(links) and set(links) <= {r["entry_id"] for r in manifest}, "Invalid reviewed line-link entries")
    result = await lookup_orders([r["reference"] for r in manifest], include_line_proof=bool(links))
    if links:
        result["reviewed_line_links"] = links
    orders = result["orders"]
    m.require(set(orders) == {"#" + r["reference"][2:] for r in manifest},
              "Metorik did not return every approved order")
    m.require(all(orders["#" + r["reference"][2:]]["order_id"] == r["order_id"] for r in manifest),
              "Metorik order mapping changed")
    return {"manifest": manifest, **result}


def validate_evidence(snapshot, evidence, payment_method="bacs", condition_code="ba"):
    m.require(payment_method in m.ROUTES and m.ROUTES[payment_method][1] == condition_code, "Unauthorized evidence route")
    h = snapshot["header"]
    m.require(evidence["entry_id"] == h["EntryID"] and evidence["reference"] == h["YourRef"],
              "Order evidence belongs to another sales entry")
    order = evidence["order"]
    m.require(type(order["order_id"]) is int and order["order_id"] == evidence["order_id"]
              and order["order_number"] == "#" + h["YourRef"][2:]
              and order["payment_method"] == payment_method, "Order payment method does not match route")
    m.require(order["currency"] == h["Currency"] == "EUR"
              and m.amount(order["total_refunds"]) == 0
              and order["status"] in ("completed", "processing", "on-hold", "pending"),
              "Order currency/refund/status requires review")
    m.require(h["PaymentCondition"] in ("PP", condition_code), "Unreviewed Exact payment condition")
    match = re.fullmatch(r"/Date\((-?\d+)\)/", h["EntryDate"])
    m.require(match is not None, "Invalid Exact entry date")
    exact_date = datetime.fromtimestamp(int(match[1]) / 1000, timezone.utc).date()
    order_dt = datetime.fromisoformat(order["order_created_at"].replace("Z", "+00:00"))
    m.require(order_dt.tzinfo is not None and order_dt.astimezone(ZoneInfo("Europe/Amsterdam")).date() == exact_date,
              "Exact invoice date and webshop date disagree")
    if m.amount(order["total"]) != m.amount(h["AmountFC"]):
        validate_reviewed_line_link(snapshot, evidence)


def validate_reviewed_line_link(snapshot, evidence):
    """Manual, explicit line-by-line identity review; never enabled by the poller.

    This is not a rounding correction: Exact amounts remain unchanged. A small
    difference alone never proves identity; every product, discount and shipping
    line must also match the same unique, date-verified live webshop order.
    """
    h, order = snapshot["header"], evidence["order"]
    m.require(evidence.get("reviewed_line_link") is True,
              "Exact original amount and order total differ; separate review")
    m.require(0 < abs(m.amount(order["total"]) - m.amount(h["AmountFC"])) <= m.amount("0.02"),
              "Reviewed line-link difference exceeds two cents")
    proof = order.get("line_proof", {})
    items, lines = proof.get("items", []), snapshot["lines"]
    m.require(items and lines, "Missing live line evidence")
    used = set()
    for item in items:
        qty, sku = item["quantity"], item["sku"]
        m.require(type(qty) is int and qty > 0 and isinstance(sku,str) and sku, "Invalid source product identity")
        matches = [line for line in lines if line["Description"] == f"{sku} ({qty}x)"
                   and m.amount(line["Quantity"]) == qty
                   and m.amount(line["AmountFC"]) == m.amount(item["total"])
                   and m.amount(line["VATAmountFC"]) == m.amount(item["total_tax"])]
        m.require(len(matches)==1 and matches[0]["ID"] not in used, "Product line identity is ambiguous")
        used.add(matches[0]["ID"])
    discount = [line for line in lines if line["ID"] not in used and m.amount(line["AmountFC"]) < 0]
    m.require(sum((-m.amount(line["AmountFC"]) for line in discount), m.amount(0)) == m.amount(proof.get("total_discount")),
              "Discount differs from webshop")
    used.update(line["ID"] for line in discount)
    shipping = [line for line in lines if line["ID"] not in used]
    m.require(len(shipping)==1 and shipping[0]["Description"] == proof.get("shipping_method_title")
              and m.amount(shipping[0]["AmountFC"]) >= 0, "Shipping/other line is not proven")
    m.require(sum((m.amount(line["AmountFC"])+m.amount(line["VATAmountFC"]) for line in lines),m.amount(0)) == m.amount(h["AmountFC"])
              and sum((m.amount(line["VATAmountFC"]) for line in lines),m.amount(0)) == m.amount(h["VATAmountFC"]),
              "Exact line totals do not reproduce the original invoice")


async def prepaid_context(api, ctx):
    rows = await api.rows("cashflow/PaymentConditions", {
        "$filter": "Code eq 'PP'", "$select": "Code,Description,PaymentMethod"})
    m.require(rows == [{"Code": "PP", "Description": "Prepaid", "PaymentMethod": "K"}],
              "Exact Prepaid condition changed or ambiguous")
    ctx["metorik_prepaid_condition"] = rows[0]


async def verify_again(evidence):
    m.require(await read_orders(evidence["manifest"], evidence.get("reviewed_line_links")) == evidence,
              "Metorik evidence changed; rebuild the read-only plan")


def item_evidence(evidence, entry_id):
    rows = [r for r in evidence["manifest"] if r["entry_id"] == entry_id]
    m.require(len(rows) == 1, "Entry is not in the explicit approved manifest")
    row = rows[0]
    result = {**row, "order": evidence["orders"]["#" + row["reference"][2:]]}
    if entry_id in evidence.get("reviewed_line_links", []):
        result["reviewed_line_link"] = True
    return result


async def evidence_plan(api, manifest, payment_method="bacs", reviewed_line_links=None):
    evidence = await read_orders(manifest, reviewed_line_links)
    ctx = await m.context(api, payment_method)
    await prepaid_context(api, ctx)
    result = {"version": m.VERSION, "division": m.DIVISION, "source": m.SOURCE,
        "destination": m.destination(ctx), "payment_method": payment_method, "created_at": m.utcnow(), "context": ctx,
        "metorik_evidence": evidence, "eligible": [], "review": [], "paid_skipped": []}
    for row in evidence["manifest"]:
        s = await m.snapshot(api, row["entry_id"])
        ev = item_evidence(evidence, row["entry_id"])
        if not s["open"]:
            result["paid_skipped"].append(row["entry_id"])
            continue
        try:
            item = m.eligible(s, ctx, ev)
            m.validate_derived_source(s, ctx, ev)
            result["eligible"].append({**item, "order_evidence": ev, "snapshot": s})
        except m.Stop as e:
            result["review"].append({"entry_id": row["entry_id"], "entry_number": s["header"]["EntryNumber"],
                "reference": s["header"]["YourRef"], "remaining": str(sum(m.amount(o["Amount"]) for o in s["open"])),
                "reason": str(e), "snapshot": s})
        print(json.dumps({"progress": "read", "reference": row["reference"]}), flush=True)
    result["plan_sha256"] = m.digest(result)
    return result
