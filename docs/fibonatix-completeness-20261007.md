# Fibonatix completeness correction — 7 October 2026

## Verified diagnosis

The original daily import was correctly read back, but `verified` described only
its eligible receipt subset. Deferred successful payments were excluded by the
standing completed-order policy and had no daily follow-up audit. The 7 October
read-only investigation compared source transaction IDs, fresh own-order status,
Exact transaction lines, all Fibonatix sales entries dated 6 October, and current
open receivables. The original imported subset was fully present without duplicate
or conflicting entries. Every sales entry in that dated scope had a source receipt.
The remaining omissions were processing orders and a refunded order. Financial
identities and per-order details remain in the existing database audit, not Git.

The three Render failure emails coincided with intentional role drains. Queue
`claim_next` detected durable draining before the heartbeat set `lease.lost`;
the consumer returned and the supervisor raised `*_task_exited_unexpectedly`.
The consumer now signals lost ownership before returning. Genuine premature
returns and exceptions still fail. No uncertain job/write is reset.

## Implementation

- `daily_review` on the existing Fibonatix worker compares the persisted verified
  source with live own-order status, Exact receipts and sales/open receivables.
- One durable read-only review per original day per local day after 01:00 is seeded
  while its receipt coverage remains incomplete. It does not download PSP source
  again or change the daily import schedule. A fresh PSP export is still required
  when investigating a late approval or a sale missing from the source; the audit
  explicitly records `source_refreshed=false`.
- `jnp_fibonatix_daily_reviews` retains daily outcomes separately from immutable
  original imports. Processing/refunded/missing/conflicting receipts remain visible.
- A reviewed snapshot is content-addressed and expires after one hour for import.
  `daily_followup_prepare`, `daily_followup_import`, `daily_followup_reconcile`
  accept `{date, revision}` under the SAME original `jnp:3977752:fibonatix:D` task.
  Follow-ups use their own durable table and the existing role fence/advisory lock.
  Prepare and import read all existing bank entries and PSP IDs again. Only proven
  missing eligible receipts are added. A previous uncertain follow-up blocks another.
  These commands are explicit; the read-only review never imports or matches.
- The reports worker records incomplete coverage when the Fibonatix audit is missing
  or incomplete and refuses to pass queued/running/uncertain work for the same day.
  Already-issued daily reports are retained, not regenerated.

## Operational continuation

During each nightly run first wait for/review the daily coverage jobs for the new
and unresolved original days. Refresh relevant PSP evidence if an approval may have
changed. Never overwrite old source/audit fixtures. For newly completed eligible
orders, use the current review revision to prepare a separate follow-up; inspect
its readback/manifest, then import, then scoped Exact Automatically. Never reset the
original verified import or an uncertain follow-up. Refresh the final coverage
readback and dashboard evidence; a stale incomplete audit cannot claim completion.

Existing completed/shipped order policy and native Automatically-only matching,
including the approved same-order EUR1 limit, remain unchanged. Processing orders
are not silently treated as completed. Refunded orders require their refund/credit
policy, never a new positive receipt merely to close a sales balance.

## Validation

Synthetic tests cover the drain race on all three queue roles, genuine worker
failures, source windows including DST, duplicates, receipt identity, processing
and refund exclusions, two-sided coverage, and a follow-up that preserves the
verified original while preparing only a newly eligible missing receipt. Production
bookings are not used as tests. Deployment and live audit results are verified
separately after a durable role drain.
