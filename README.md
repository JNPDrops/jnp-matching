# JNP Matching v0.8

Read-only AllocationRule preview improvement.

- Resolves debtor 100100 dynamically by scanning ReceivablesList client-side.
- Falls back to TransactionLines client-side scan.
- Does not hardcode the Exact account GUID.
- Shows the discovered GUID and evidence on the preview page.
- Keep `ENABLE_ALLOCATION_RULE_WRITES=false` and `ENABLE_DIRECT_MATCH_WRITES=false` during validation.
