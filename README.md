# JNP Matching v0.7

Read-only diagnostic fix for Exact Online. The `/diagnose/{order}` endpoint now uses explicit `$select` fields on `ReceivablesList` and `TransactionLines`, as required by Exact Online.

Keep write flags disabled while diagnosing:
- `ENABLE_ALLOCATION_RULE_WRITES=false`
- `ENABLE_DIRECT_MATCH_WRITES=false`
