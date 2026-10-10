# Unattended processing: implementation checkpoint

Status: **draft, incomplete, not safe to deploy as the full requested feature**.
The additions in this branch do not enable scheduled financial processing.

## Accepted schedule

Europe/Amsterdam: 09:00 / 13:00 / 17:00 / 21:00, with immutable exclusive
cutoffs 08:00 / 12:00 / 16:00 / 20:00 on the same calendar date. The 01:00
slot covers the entire preceding calendar date. The source window begins at
that processing day's local midnight. Overlapping observations are intentional;
PSP transaction identity, not a window boundary, determines deduplication.
Late source changes and incomplete older days need fresh separate revisions.

The new registration module keeps every missed slot and its original cutoff.
It does not change or reuse completed daily imports. Calendar dates rather than
24-hour subtraction handle the 23/25-hour days around clock changes.

## Routing prerequisite implemented in this branch

The existing router's normal interval remains 60 seconds, with a 15-second
poll while processing its queue (subject to shared API budget). A scan cursor
alone is not success. Only reaching the successful end of a cycle with the
current assigned routing lease writes a completion record.

The existing generic daily Fibonatix/ICEPAY upload paths now check a successful
cycle completed within 15 minutes, and coverage through the day cutoff, before
claiming a new financial upload. The PSP native Automatically paths check recent
routing and pending/uncertain queue entries for their selected references.
The global ICEPAY wrong-order safeguard remains unchanged.

The native PSP paths now read the actual own Sales Entry and remaining open item
across all debtors before the click. The guard rejects wrong debtors, missing or
ambiguous invoices, credits/reversals, non-EUR amounts and differences above EUR 1.
Trustworthy exclusion of affected invoice/payment chains is still required before
replacing the global safeguard. Merely excluding a bank
receipt can leave its invoice available as a wrong counterparty to another receipt.

## Notification component

`processing_alerts.py` provides a deduplicated durable outbox and explicit
uncertain-delivery state. Microsoft Graph is the default transport, using
`processing_graph_mail.py` with app-only tokens. No browser or dashboard session
is used. `202 Accepted` is recorded as `accepted`, never verified delivery.
An ambiguous response is not retried automatically. No email has been sent.
Sender wiring/configuration is not complete.

An administrator must authorize Application Mail.Send through Exchange Online
RBAC, limited to the selected sender mailbox. Do not grant tenant-wide sending
or reuse/expand the dashboard application without explicit review and consent.
Configure JNP_GRAPH_TENANT_ID, JNP_GRAPH_CLIENT_ID, JNP_ALERT_FROM, JNP_ALERT_TO
and exactly one of JNP_GRAPH_CERTIFICATE_PATH or JNP_GRAPH_CLIENT_SECRET.
For PFX certificates, JNP_GRAPH_CERTIFICATE_PASSWORD is optional. Prefer a
managed certificate rotation procedure; no credential is permanent. The mail
recipient is the address authorized in the conversation, not stored in Git.

An existing SMTP account remains an explicit `JNP_MAIL_PROVIDER=smtp` fallback
with JNP_SMTP_HOST, JNP_SMTP_PORT (TLS 465), JNP_SMTP_USER and JNP_SMTP_PASSWORD.
Do not assume Outlook supports basic SMTP authentication.

Microsoft documentation:
- https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow

## Blocking work before activation

1. Add reusable bounded fresh Paragon acquisition under the assigned Fibonatix
   role. Main currently has source validation and consumes stored exports, but
   no `daily_source` Fibonatix dispatcher. Preserve source versions.
2. Add separate intraday PSP import storage and validation using the new batch
   key, cutoff and source revision. Current daily adapters explicitly reject
   the current day and their primary keys allow one original import per day.
   Do not delete these safeguards or repurpose historical imports.
3. Wire the durable schedule into one existing assigned worker; implement
   dependency progression using verified adapter results, not queued/completed
   job labels alone. Respect the existing global chain lock and role write fences.
4. Add completion boundaries for continuous Woo, tax and maintenance work;
   validate the new own-invoice readback against a production read-only scope. Preserve
   the user's allowance for a fully settled swap as an auditable exception,
   never infer settlement from an empty UI list.
5. Wire failure/missing-start/deadline alerts to the outbox and poll its sender.
   A second existing worker must watch the coordinator heartbeat; a dead
   coordinator cannot send its own missed-run alert. Retain platform alerts.
6. Configure/test existing email delivery to the approved recipient. Do not
   create a new provider account or infrastructure without authorization.
7. Verify database migrations and adapter/worker integration tests, drain affected
   roles, deploy routing instrumentation first, prove its live completion record,
   then deploy consumers. Preserve all prior uncertain and completed operations.
8. Run a live read-only preflight and bounded authorized batch; verify source
   completeness, Exact readback, downstream gates and final report. Retire the
   paused chat executor only after autonomous processing is proven.

Render Web Shell authentication was restored on 2026-10-10. Internal read-only
database access works. Keep the database external allowlist closed. This removes
the management-access blocker, not the remaining implementation blockers above.
Never deploy merely to force a restart or drain.

## Validation performed

Synthetic tests cover local cutoffs, daylight-saving transitions, delayed slots,
timezone-equivalent identity, routing freshness and cutoff coverage, and safe
notification construction. These tests do not establish production readiness.
App-only mail tests additionally cover bounded single submission, authentication
failure before durable send intent, committed intent before network submission,
sanitized errors, and preservation of uncertain delivery. The local processing
and mail test set contains 20 passing tests. Full CI must pass on the final head.
