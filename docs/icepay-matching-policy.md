# ICEPAY matching policy — 6 October 2026

Jasper's standing instruction: match through **Automatically in Exact**. Do not
create cross-account entries to offset orders against each other. This replaces
the previous manual per-order matching procedure for the ICEPAY worker.

`operations.icepay_automatic` runs on the existing assigned ICEPAY worker. It
checks every 15 minutes, starting immediately after deployment. Each round is a
durable queue job; it uses the same ownership, drain and write-intent controls as
the other worker tasks. A click whose outcome is uncertain blocks further rounds.

The action is limited to administration 3977752, the existing ICEPAY bank
account/journal 27, positive EUR order receipts on debtor 109419 and GL 1100.
The worker resets remembered UI filters, selects open `ICEPAY TD… | Payment …`
receipts and invokes the native Automatically button. It creates no offset,
write-off, repair or journal entry itself. Exact decides which receipts it can
match; the remainder stays open. Before/after evidence and remaining receipts
are retained privately in `jnp_icepay_automatic_runs`.

Old import task dates, claims and expiry times are unchanged. This policy does
not replay a previous import or authorize importing the same payment twice.
Historical readback remains available, but manual ICEPAY save operations and
their old group commands are retired. Payout and refund accounting is separate
from matching webshop receipts.
