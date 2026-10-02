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


async def read_orders(manifest):
    manifest = manifest_rows(manifest)
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
        wanted = {"#" + r["reference"][2:]: r for r in manifest}
        orders = {}
        numbers = list(wanted)
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
                    number = row["order_number"]
                    m.require(number in batch_numbers and number not in orders
                              and row["order_id"] == wanted[number]["order_id"],
                              "Metorik order mapping missing, changed or ambiguous")
                    orders[number] = row
                if not pg["has_more_pages"]:
                    break
                m.require(bool(rows), "Empty intermediate Metorik page")
            else:
                raise m.Stop("Incomplete Metorik pagination")
        m.require(set(orders) == set(wanted), "Metorik did not return every approved order")
    return {"manifest": manifest, "store": store, "orders": orders}


def validate_evidence(snapshot, evidence):
    h = snapshot["header"]
    m.require(evidence["entry_id"] == h["EntryID"] and evidence["reference"] == h["YourRef"],
              "Order evidence belongs to another sales entry")
    order = evidence["order"]
    m.require(type(order["order_id"]) is int and order["order_id"] == evidence["order_id"]
              and order["order_number"] == "#" + h["YourRef"][2:]
              and order["payment_method"] == "bacs", "Order is not proven bacs")
    m.require(order["currency"] == h["Currency"] == "EUR"
              and m.amount(order["total_refunds"]) == 0
              and order["status"] in ("completed", "processing", "on-hold", "pending"),
              "Order currency/refund/status requires review")
    m.require(h["PaymentCondition"] in ("PP", "ba"), "Unreviewed Exact payment condition")
    match = re.fullmatch(r"/Date\((-?\d+)\)/", h["EntryDate"])
    m.require(match is not None, "Invalid Exact entry date")
    exact_date = datetime.fromtimestamp(int(match[1]) / 1000, timezone.utc).date()
    order_dt = datetime.fromisoformat(order["order_created_at"].replace("Z", "+00:00"))
    m.require(order_dt.tzinfo is not None and order_dt.astimezone(ZoneInfo("Europe/Amsterdam")).date() == exact_date,
              "Exact invoice date and webshop date disagree")
    m.require(m.amount(order["total"]) == m.amount(h["AmountFC"]),
              "Exact original amount and order total differ; separate review")


async def prepaid_context(api, ctx):
    rows = await api.rows("cashflow/PaymentConditions", {
        "$filter": "Code eq 'PP'", "$select": "Code,Description,PaymentMethod"})
    m.require(rows == [{"Code": "PP", "Description": "Prepaid", "PaymentMethod": "K"}],
              "Exact Prepaid condition changed or ambiguous")
    ctx["metorik_prepaid_condition"] = rows[0]


async def verify_again(evidence):
    m.require(await read_orders(evidence["manifest"]) == evidence,
              "Metorik evidence changed; rebuild the read-only plan")


def item_evidence(evidence, entry_id):
    rows = [r for r in evidence["manifest"] if r["entry_id"] == entry_id]
    m.require(len(rows) == 1, "Entry is not in the explicit approved manifest")
    row = rows[0]
    return {**row, "order": evidence["orders"]["#" + row["reference"][2:]]}


async def evidence_plan(api, manifest):
    evidence = await read_orders(manifest)
    ctx = await m.context(api)
    await prepaid_context(api, ctx)
    result = {"version": m.VERSION, "division": m.DIVISION, "source": m.SOURCE,
        "destination": m.DESTINATION, "created_at": m.utcnow(), "context": ctx,
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
