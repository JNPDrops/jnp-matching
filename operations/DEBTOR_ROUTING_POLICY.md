# Debtor routing authorization — 3 October 2026

Administration: 3977752. Source debtor: 100100. Existing service and database only.
Policy revision: `2026-10-03-all-routes-v1`.

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
and all uncertain write outcomes. It waits until **4 October 2026 at 02:00:05
Europe/Amsterdam** (00:00:05 UTC). This is a start time, not a completion guarantee.
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

Every PUT checks the persistent enabled flag and policy revision. The worker
respects the Exact response budget/reset headers, reserves 100 calls and resumes
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
