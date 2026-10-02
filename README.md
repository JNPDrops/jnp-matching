# JNP Matching v1.4

Adds a consolidated **read-only** diagnostic endpoint for one exact imported bank line:

`GET /api/diagnostics/bank-line/{bank_line_id}?receivable_entry=...`

The endpoint uses GET requests only and returns the BankEntryLine identity, parent BankEntry, TransactionLines by EntryID, target receivable evidence, duplicate-amount checks, allocation state, and computed MatchSets preconditions. It never calls POST/PUT/DELETE, AllocationRule writes, or MatchSets upload.

The development agent regression suite now checks the golden case (order 48451 / bank-line GUID `591003b9-cfb0-4158-b23c-fdc106801a5e`) and expects `allocation_required=true` and `matchsets_eligible=false` while the line remains on G/L 1360.

Keep all financial write flags disabled.
