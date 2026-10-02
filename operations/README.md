# Eenmalige bacs-debiteurenomzetting

Expliciete operatoractie voor Exact-administratie **3977752**, bestaande debiteur
**100100 → 109372**. Geen webroute, startup-hook of periodieke taak. De bestaande
matchinginstellingen, Xcore en DIRECT_WOO_BANK worden niet veranderd.

De **betalingsconditie van de verkoopboeking** is bepalend. De unieke Exact-conditie
met omschrijving exact `bacs` wordt opgezocht. Een oudere `PP`-conditie op de
cashflowpost sluit de boeking niet uit en wordt niet opzettelijk gewijzigd.
Alle bacs-verkoopboekingen op 100100 worden volledig gepagineerd opgehaald,
zonder datumfilter. Werkelijke restbedragen komen uit Exact ReceivablesList,
gecontroleerd tegen Cashflow Receivables. Geen Metorik of WooCommerce.

Uitvoering is beperkt tot volledig open verkoopboekingen (status/type 20), met
één open cashflowrecord, unieke TD-orderreferentie en bijpassende omschrijving.
Gedeeltelijk betaalde, gesplitste, gematchte, verwerkte, credit-, dubbele of
onduidelijke boekingen worden voor beoordeling gerapporteerd. Bestaande debiteuren
moeten uniek worden gevonden. De routine maakt geen debiteur aan.

## Handmatige uitvoering op de bestaande Render-service

Controleer bestaande deployments; deploy de goedgekeurde commit eenmaal handmatig.
Maak daarna in de bestaande service-shell een actueel plan:

```sh
python -m operations.bacs_debtor_transfer plan --output /tmp/bacs-plan.json
```

Lees het resultaat en de concrete entry-ID's, bedragen en uitzonderingen. Een
plan is maximaal 30 minuten geldig. De SHA verwijst naar de volledige inhoud.

```sh
python -m operations.bacs_debtor_transfer apply \
  --plan /tmp/bacs-plan.json --expect-sha256 SHA_UIT_HET_PLAN \
  --audit /tmp/bacs-audit.jsonl
```

Gebruik een nieuw pad per plan/poging. Een bestaande audit wordt nooit overschreven.
Alle geselecteerde boekingen worden vóór de eerste PUT en opnieuw vlak vóór hun
eigen PUT gelezen. De enige financiële mutatie is:

`PUT /api/v1/3977752/salesentry/SalesEntries(guid'ENTRY_ID')`

met uitsluitend `{"Customer":"BESTAANDE_DOEL_GUID"}`. Een HTTP-fout of onzekere
netwerkuitkomst wordt niet herhaald. Geen fallback, herimport, aflettering,
memoriaalboeking of automatische rollback.

Na iedere PUT moeten verkoopboeking, verkoopregels, financiële regels, cashflow
en openstaande post overeenkomen met het vooraf vastgelegde beeld, met alleen
de verwachte debiteurwijzigingen. Bedragen, btw, referenties, betalingscondities,
valuta, vervaldata en overige gecontroleerde velden moeten gelijk blijven.
Daarnaast wordt de volledige open-postenpopulatie van beide debiteuren voor en
na vergeleken. Gelijktijdige Xcore/gebruikerswijzigingen kunnen deze globale
controle laten stoppen; onderzoek dan de audit en herhaal geen PUT blind.

De 0600-plan- en auditbestanden bevatten financiële gegevens, geen geheimen.
Bewaar de audit vóór een nieuwe deployment; `/tmp` is niet duurzaam. Deze
bestanden mogen niet in git. Een succesvolle unit-test bewijst niet dat Exact
een concrete boeking accepteert: alleen de live teruglezing bewijst het resultaat.

Officiële routes en velden, geraadpleegd 2 oktober 2026:
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=SalesEntrySalesEntries
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=SalesEntrySalesEntryLines
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialTransactionTransactionLines
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=CashflowReceivables

## Tests

```sh
python -m unittest operations.test_bacs_debtor_transfer -v
```
