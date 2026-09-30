# JNP Matching v0.6

Read-only diagnostic release.

New endpoint: `/diagnose/48451`

It only performs GET requests to Exact Online and shows the raw `ReceivablesList` record for `TD48451` plus the related `TransactionLines`. This lets us identify the exact debtor account GUID/field names used in this Exact administration without guessing.

Keep both write flags disabled:

- `ENABLE_ALLOCATION_RULE_WRITES=false`
- `ENABLE_DIRECT_MATCH_WRITES=false`
