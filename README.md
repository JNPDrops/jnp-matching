# JNP Matching v0.2

Exact Online matcher voor webshopbetalingen.

## Veilige opbouw

- Dry-run: bankregels op 1360 -> ordernummer -> `TD{order}` -> openstaande post.
- Preview: leest de daadwerkelijke transactieregels en toont de boeking die nodig is.
- Write lock: standaard `ENABLE_MATCH_WRITES=false`.
- Write route (na expliciet inschakelen):
  1. maakt een algemene journaalboeking: debet 1360 / credit debiteurenrekening + verzameldebiteur;
  2. lettert de nieuwe debiteurenregel af tegen de openstaande verkooppost via Exact XML `MatchSets`.

## Waarom een extra memoriaalboeking?

De Exact REST resource voor bestaande `BankEntryLines` ondersteunt geen normale PUT om een bestaande bankregel van 1360 naar een debiteur te verplaatsen. Daarom wordt de bankboeking zelf niet aangepast. De tussenrekening wordt via een transparante memoriaalboeking leeggemaakt en vervolgens wordt de debiteurenregel afgeletterd.

## Belangrijk

Laat `ENABLE_MATCH_WRITES=false` totdat de preview van minimaal één READY-regel boekhoudkundig is gecontroleerd. Gebruik daarna eerst één kleine betaling als productieproef.
