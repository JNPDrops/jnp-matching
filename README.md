# JNP Matching v0.4

Fix voor de previewfout `Kon de unieke 1360-transactieregel niet bepalen (0 kandidaten)`.

De dry-run werkt op `BankEntryLines`; daarom gebruikt de preview nu exact dezelfde bankregel als bron in plaats van te proberen de 1360-regel opnieuw terug te vinden via `TransactionLines`.

Voor inkomende webshopbetalingen geldt in de preview:
- 1360: debet voor het ontvangen bedrag
- debiteur: credit voor hetzelfde bedrag

`ENABLE_MATCH_WRITES` blijft standaard `false`. Zet dit nog niet aan voordat de preview van order 48451 is gecontroleerd.
