# Debtor routing authorization — 3 October 2026

Administration: 3977752. Source debtor: 100100. Existing service and database only.
Policy revision: `2026-10-03-allocation-icepay-now-v2`.

| Exact-linked order payment code | Destination | Scope |
| --- | --- | --- |
| `wc_fibonatix` / `wc_fibonatics` | 100100 | Retain; never transfer |
| `bacs` | 109372 | Current open items and future imports |
| `plisio` | 109377 | Current open items and future imports |
| `icepay-ideal` | 109419 | Current open items and future imports |
| `np_payments` | 109421 | One-time open-item cleanup |
| `suap_wordpresspayplugin` | 109422 | One-time open-item cleanup |

`icepay-ideal` is the observed order code. Do not infer unobserved aliases or
identify SUAP as a particular provider. Destination debtors must already exist
uniquely and be active sales accounts; they are never created.

## Schedule and progress

The worker activates this authorization once, preserving the incremental cursor
and all uncertain write outcomes. The 21:34 operator instruction brings the
historic open ICEPAY cohort forward to run immediately. At 22:00 the operator
also authorized immediate SUAP cleanup. At 22:01 they requested using the revised
credit inventory from the other chat. At 22:20 they authorized immediately
continuing the remaining cleanup routes after SUAP. Prioritize ready SUAP cleanup
entries, then drain the other methods, preserving the same completed discovery
cohort. An uncertain SUAP write stays isolated and does not block the other work.
Continuous imports start **4 October 2026 at 02:00:05 Europe/Amsterdam**
(00:00:05 UTC). This is a start time, not a completion guarantee.
A later persistent operator pause must remain effective across cycles/restarts.

At start, read the complete current ReceivablesList for 100100 without an invoice
age cutoff. Persist that cohort and discovery cursor. Read sale headers in groups
of entry numbers and obtain order evidence from the existing Metorik reader.
Incremental scans use Modified, so newly imported old-dated invoices are included.
Normal polling is every 300 seconds; ready work drains in batches of 50 with a
15-second interval. Do not repeat incremental scans on every drain cycle.

Credit references are not order references. A credit must explicitly name its
original order, have a unique corresponding debit sale, and agree with the order
currency, original amount and recorded refund. Unknown or ambiguous credits stay
unclassified. Future credit automation is not enabled by this policy.

## Execution

Reuse the established order link, read the individual current sale header and
open remainder, then send one PUT containing only `Customer`. Nonzero partial
remainders are allowed. Positive sale balances and negative proven credit
balances are handled according to the entry type. No balance comparisons,
post-write rereads, matching, reimports, new debtor creation, or amount/VAT/reference
changes. A write intent is persisted before PUT. Ambiguous outcomes remain
isolated and are never automatically retried.

Every PUT checks the persistent enabled flag and policy revision. The worker uses **JNP Allocation** permanently; the primary connection remains
assigned to the existing matching/rule functionality. It respects the Exact response budget/reset headers, reserves 100 calls and resumes
after the reset. No extra OAuth app registrations or key rotation are used to
multiply quotas. Other integrations also consume the administration-wide budget.

Health reports policy, schedule, active routes, cleanup progress and the last
observed API headers, without secrets. `applied_since_start` resets on service
restart; durable queue counts and the audit retain per-entry outcomes. Watch for
`scheduled`, `processing_queue`, `watching`, `waiting_for_api_budget`, `disabled`
and `retry_next_cycle`. Unknown mappings and validation failures require review.

Xcore, DIRECT_WOO_BANK and the Woo/bosci worker are unchanged. Do not call
`POST /cycle` or `POST /research` for this operation. Deploy manually after checking
existing deployments and confirming auto-deploy remains off.

Before the scheduled start, both queue selection and the last write guard require
a durable cleanup entry with one of the authorized payment codes and its exact
destination from the route table. Each destination is bound to that entry's
stored payment code. Continuous entries still wait for the scheduled start.
Health exposes counts by payment method and the latest observed
Allocation budget headers. It performs no financial post-write rereads.

## SUAP reassessment incorporated at 22:01

The revised `openstaande-posten-creditanalyse-20261003.xlsx`, version 2, updated
19:48:53 UTC, identifies 343 SUAP entries in the 09:35 Exact snapshot. Its minimal
entry/order identity list is persisted in `reviewed_suap_20261003.json`.
TD113933 belongs to order TD41300 (Metorik 111850); TD116518 belongs to TD40287
(Metorik 108478). The former proposed TD113933/40287 pairing is rejected.

Compare that reviewed inventory with the existing live cleanup queue before
processing SUAP. The comparison is local database work and uses no Exact calls.
Any conflicting entry/order identity is held for review. Absence from the live
queue is reported with its discovery reason; the spreadsheet does not override
current open status, debit/credit proof, completed work or uncertain writes.
Keep the cohort revision unchanged to reuse discovery and preserve the 25
already applied ICEPAY entries. SUAP remains a one-time open-item cleanup.

## Remaining cleanup authorized at 22:20

NinjaPay to 109421, BACS to 109372 and Plisio to 109377 may now run immediately
after SUAP. The same inventory's changed and still unknown entries are included
in `reviewed_routing_updates_20261003.json`. Each affected queued identity must
agree with that review before writing; unknown payment methods remain excluded.
Orders absent from the older snapshot continue to use their established live
order evidence. No uncertain writes, completed transfers or unproven credits are
requeued by this change. The review's amount differences are not corrected.
