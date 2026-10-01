# JNP Matching v1.0

Read-only bank-first reconciliation for Exact Online division 3977752.

The primary route is `/candidates`:
1. Read bank lines still on suspense GL 1360.
2. Extract a possible WooCommerce order number from the bank description.
3. Look up exactly `TD<order>` in `ReceivablesList`.
4. Require account code 100100 and exact positive amount match.
5. Label the row `MATCH_CANDIDATE` or a review status.

No allocation rules are created and no bank or financial entries are modified by this route.
Keep `ENABLE_ORDER_RULE_WRITES=false` and `ENABLE_DIRECT_MATCH_WRITES=false` while validating the candidate list.
