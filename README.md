# JNP Matching v0.4

Deze versie verwijdert de memoriaal-workaround uit het write-pad.

## Nieuwe flow
1. Exact AllocationRule wijst toekomstige bankregels met `TD` in de omschrijving toe aan verzameldebiteur `100100`.
2. JNP Matching leest alleen bankregels die al aan `100100` zijn toegewezen.
3. De matcher zoekt de openstaande post via `TD{ordernummer}` en controleert exact bedrag + unieke match.
4. Alleen daarna kan Exact XML MatchSets de twee bestaande debiteurenregels direct afletteren.

## Veiligheidslocks
- `ENABLE_ALLOCATION_RULE_WRITES=false`
- `ENABLE_DIRECT_MATCH_WRITES=false`

Zet ze niet tegelijk aan tijdens de eerste tests.

## Belangrijk
De AllocationRule werkt alleen als toekomstige bankomschrijvingen het herkenningswoord bevatten, standaard `TD` (bijv. `TD48451`). Bestaande 1360-regels worden alleen geanalyseerd en moeten voorlopig handmatig in Exact worden verwerkt.
