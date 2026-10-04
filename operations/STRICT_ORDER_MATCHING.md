# Strict source-order matching

`strict_order_matching.py` extends the existing authenticated, expiring import controller. It never accepts arbitrary URLs, scripts, accounting identities, import payloads, or write-off amounts. No task starts during deployment. The broad Automatically route stays blocked and direct matching API writes remain disabled.

## Operator sequence

1. `POST strict/prepare` reads the full journal and invoice history, including closed invoices and different debtors. It stores the repair plan privately.
2. `POST strict/inspect` reads the actual selected invoice for a bounded receipt.
3. `POST strict/undo?limit=N` undoes only a proven cross-order selection. It verifies the reopened receipt and original invoice through the UI and receivables API.
4. `POST strict/match?limit=N` selects a unique invoice belonging to the source order, with verified administration, debtor, currency, invoice identity, and outstanding amounts. Process available own invoices between undo batches to release chains without a broad matching action.
5. If interrupted after a save claim, `POST strict/recover` only reads Exact. Never repeat the save. Further financial processing is blocked while an outcome remains unknown.

The limit is at most 20 per invocation. The plan cannot be overwritten once a save was attempted. A durable claim precedes every save. A verified source receipt retains its bank amount, offset amount, original description and transaction identity. Exact clears YourRef when a match is undone; the original source order remains in the description and private plan, and matching the own invoice restores its reference.

## Exceptions and decisions

Missing/ambiguous invoices, another debtor, multiple receipts for one order, partial payment and unapproved differences remain explicit exceptions. Searching only open receivables is insufficient. A previously approved difference is accepted only from its private decision dossier, with exact order, invoice, amount, actor, time, and case-only scope. Undo verifies its reversal before moving the approved difference to the original order's receipt, preventing duplicate write-offs.

The private `strict_order_plan` artefact contains source identities, invoice identities, state, evidence, attempts, workflow status, assignment, suggested action and separate execution verification. Connect these fields to the central dashboard worklist described in DASHBOARD_EXCEPTIONS.md. A recorded decision is not a completed Exact action.

## Scope and verification

This is an operator-started workflow for the currently authorized import dossier. It is not a daily scheduler or a new PSP importer. Historical repair completion must be reported from verified results, not from deployment or a zero debtor balance. No personal transaction or invoice dossier belongs in the public repository.

Synthetic tests cover equal amounts across orders, duplicate invoices, different debtors, partial matches, original source preservation, unique transaction identity, and nontransferable difference approval. Live evidence remains private.
