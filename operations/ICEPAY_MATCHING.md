# ICEPAY 1–3 October 2026 own-order matching

Authorized by Jasper on 4 October: “wil je de aflettering nog even doen”.
Scope: the 36 receipts independently verified by `icepay_apply`, journal 27,
debtor 109419, division 3977752, EUR 3,161.24. No payouts or fees are included.

`ICEPAY_MATCH_TASK_ID` explicitly activates one expiring, once-claimed task:

- `icepay-match-20261001-03-prepare-v1`: read-only invoice inventory, including
  closed invoices and invoices on other debtors.
- `icepay-match-20261001-03-group-01-v1` through `group-08-v1`: at most five
  native Exact saves per invocation. Operator reviews each run before the next.
- `icepay-match-20261001-03-recover-v1`: read-only recovery of uncertain saves.
- `icepay-match-20261001-03-verify-v1`: final read-only ledger/open-item check.

No ordinary startup activation, unattended batch loop, generalized accounting
endpoint, or configurable identity. All task IDs expire 5 October 18:00 UTC.
The existing Render activation deployment is the operator control; no second
deployment is needed after an environment-variable merge.

Each save requires one own TD order, one full matching EUR invoice on 109419,
one unchanged receipt, zero amounts in transit, exact UI frame identities,
and no write-off. Invoice history is read across debtors. Ambiguity, closed
invoices, differences and partial payments remain explicit exceptions.

Private evidence is stored in `icepay_order_matching`; activation receipts in
`icepay_matching_runs`; never-reset per-bank-line claims in
`icepay_matching_saves`. Save claim and unknown-outcome state commit atomically
before the native Save click. Uncertain outcomes block all further writes.
Verification requires the selected own invoice in the native UI, closed invoice
and receipt in the API, and unchanged imported ledger lines.

The Fibonatix login and UI reading helpers are reused without changing any
Fibonatix constants or writing its task evidence. Existing dashboard integration
is not modified; ICEPAY exception records include source and invoice identity,
amounts, reason, workflow state, suggested action, decision and execution state.
