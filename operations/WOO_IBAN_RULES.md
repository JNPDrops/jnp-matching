# WooCommerce allocation rules

The Moneybird Order Match plugin signs receipt evidence after successful completion.
The existing service queues it in `jnp_woo_iban_events` and creates an Exact beta
`cashflow/AllocationRule` with only `Account` (debtor 109372) and
`Words` (the first 35 characters of the full Moneybird `bosci_` reference).
GoDutch CAMT stores this shortened reference in `AcctSvcrRef`. The user confirmed
a words-rule import test on 2026-10-03. MT940 omits this reference and is not the
supported import for the new flow. Division 3977752 is fixed. No payment, invoice,
bank-entry or existing-rule mutation is performed by this module.

Configuration in the existing Render service:

- `WOO_IBAN_SHARED_SECRET`: a random 32-byte hex string, also entered in WordPress.
- `ENABLE_WOO_IBAN_RULE_WRITES=true`: enable queue processing.
- `REPLACE_ORDER_RULES_WITH_IBAN=true`: reject the old order-number-rule creation
  route with HTTP 410. Existing Exact rules are deliberately not deleted.

Never put the shared secret in Git or the installable WordPress ZIP. It is sent
to the user separately. WordPress encrypts it using its existing token storage.
Both deployments keep all existing debtor sales-entry routing intact.

POST `/api/woocommerce/allocation-rule` with the receipt evidence body: use
`bank_reference` (exactly `bosci_` plus 32 lowercase hex digits) instead of `iban`.
The plugin extracts one distinct full reference from `message` and/or
`account_servicer_transaction_id`; missing or conflicting references require review.
The receiver validates the full reference and derives the 35-character Words value.
No IBAN, amount, or date criterion is added to the Exact rule. Headers:
`X-MBOM-Timestamp` (Unix seconds) and `X-MBOM-Signature` (hex HMAC-SHA256 over
timestamp + `.` + exact request bytes). Five-minute replay window; durable
transaction and order uniqueness make repeated signed submissions idempotent.
Authenticated repeats also return current job status. A different evidence body
for an existing event or a second transaction for the same order is rejected.

The legacy POST `/api/woocommerce/iban-rule` remains compatible with old clients
and previously dispatched immutable IBAN receipts. The durable table, feature
flags and secret retain their original names. Both routes share receipt uniqueness.
Do not rewrite or resubmit legacy dispatched evidence as reference evidence.

POST `/api/woocommerce/iban-rule/check` signs the fixed body `{}`. This checks
debtor resolution and allocation-rule read access without creating a rule. It also returns `allocation_rule_version: 2` and
`rule_types: ["iban", "bosci_words"]`.

States: pending, creating, done, conflict, uncertain. `done` requires an Exact
readback. An identical manually created test rule is reused. A conflicting rule is never overwritten. A write timeout/crash
leaves durable intent; subsequent runs look for the result but never blindly
reissue the POST. Investigate persistent uncertain/conflict records with a
private DB read. After operator investigation, pending may be restored only if
the original attempt is conclusively known not to have created the rule.

The historical WordPress action scans imported Moneybird transactions and finds
paid BACS orders created on/after 2026-10-01. It uses the plugin's existing
confirmed association, or a unique order-number/full-name + exact EUR amount
match. No historical WooCommerce payment is replayed. On upgrade, the plugin starts one historical scan and upgrades only unsent
`missing_iban` / `missing_reference` receipts. No historical payment_complete call
is performed. Missing/ambiguous references and uncertain matches require review; the action cannot infer payments not present in Moneybird.
Historical scans may be rerun after further Moneybird imports; duplicate events
are suppressed. Run the bank import only after the relevant jobs are done.

Tests: `python -m pytest operations -q`. The PHP ZIP contains separate matching,
payment-contract, token and queue tests; no live financial writes are used in tests.
