# Eenmalige bacs-debiteurenomzetting

Expliciete operatoractie voor Exact-administratie **3977752**, bestaande debiteur
**100100 → 109372**. Geen webroute, startup-hook of periodieke taak. De bestaande
matchinginstellingen, Xcore en DIRECT_WOO_BANK worden niet veranderd.

De **betalingsconditie van de verkoopboeking** is bepalend. De unieke Exact-conditie
met omschrijving exact `bacs` wordt opgezocht. Een oudere `PP`-conditie op de
cashflowpost sluit de boeking niet uit. Exact kan die afgeleide conditie bij een
Customer-wijziging opnieuw opbouwen; zie de expliciete goedkeuringsoptie hieronder.
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

### Goedgekeurde Exact-nevenwijzigingen

De eerste live uitvoering stopte terecht: Exact regenereerde aanvullende velden.
Op 3 oktober 2026 is expliciet toestemming gegeven om de resterende omzettingen
met deze nevenwijzigingen af te maken. Gebruik daarvoor bij `apply` de optie
`--accept-exact-derived-changes`. Deze keuze wordt in de audit opgeslagen en
staat standaard uit. Alleen de volgende extra veranderingen zijn toegestaan:

- Nieuwe cashflow-ID/EntryID en HID van de geselecteerde openstaande post.
  Koppelingen naar de verkoopboeking en financiële transactieregel blijven gelijk.
- Cashflowconditie exact `bacs` (code van de unieke conditie, methode B).
  Vooraf moet de conditie bacs of PP/Prepaid/K zijn.
- Het gegenereerde betalingskenmerk `100100/BOEKING` wordt `109372/BOEKING`.
  Een afwijkend eigen kenmerk blokkeert vóór de eerste PUT.
- Alleen op de btw-samenvattingsregel 9999 mag de vervaldatum leeg worden;
  het regelbedrag moet het tegengestelde van het btw-totaal zijn. Factuur-,
  debiteuren- en overige vervaldata blijven strikt gelijk.

Er worden geen extra velden naar Exact geschreven: de PUT blijft Customer-only.
Bedragen, btw, verkoopregels, valuta, order-/factuurreferenties en alle overige
gecontroleerde gegevens moeten ook met deze optie gelijk blijven.

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
# Aanvullende eenmalige selectie: bewezen Metorik-bacs

Na expliciete toestemming voor de overige bacs-webshoporders kan `plan` een
`--metorik-manifest /tmp/approved-bacs.json` ontvangen. Dit is een lijst met uitsluitend
`entry_id`, `reference` (TD-nummer) en `order_id`. Het manifest bevat geen geheimen en
wordt niet in Git opgeslagen. Er is geen brede selectie van alle Prepaid-boekingen.

De planner leest de genoemde orders opnieuw uit de bestaande Metorik-winkel
TheDrops.eu, zonder filter op betaalmethode. Iedere referentie en order-ID moeten
uniek overeenkomen; de betaalmethode moet exact `bacs` zijn. Valuta, factuurdatum,
oorspronkelijk bedrag, refundstatus en Exact-restbedrag worden gecontroleerd.
Vóór uitvoering wordt de Metorik-bewijsset opnieuw gelezen. De bestaande controles
op Exact-status, één volledig open post, btw, boekingsregels en totaalsaldi blijven
gelden. Deelbetaalde/afgeletterde posten worden afzonderlijk gerapporteerd.

De enige wijziging blijft `SalesEntries.Customer`. Een aanwezige Exact-betaalconditie
PP blijft PP; de nacontrole verwacht dat Exact de opnieuw gegenereerde cashflow aan
de ongewijzigde verkoopconditie koppelt. `--accept-exact-derived-changes` blijft
nodig voor de expliciet goedgekeurde nevenwijzigingen. Dit voegt geen automatische
verwerking, webhook, Plisio-route, infrastructuur of aflettering toe.

## Plisio, dezelfde controles

Dezelfde executor ondersteunt nu de vaste routes `bacs → 109372` en
`plisio → 109377`, steeds uitsluitend vanaf 100100 in administratie 3977752.
De bestemmingsdebiteur wordt live uniek en actief gecontroleerd; een route kan
niet via het plan naar een willekeurig rekeningnummer worden omgebogen.
Plisio vereist expliciete, opnieuw live gelezen Metorik-orderbewijzen. De
verkoopconditie mag PP/Prepaid of pl/plisio zijn. Een PP-header blijft PP; alleen
Customer wordt geschreven. De afgeleide referentie gebruikt doel 109377.

```sh
python -m operations.bacs_debtor_transfer plan --payment-method plisio \
  --metorik-manifest /tmp/plisio-manifest.json --output /tmp/plisio-plan.json
python -m operations.bacs_debtor_transfer apply \
  --plan /tmp/plisio-plan.json --expect-sha256 SHA_UIT_HET_PLAN \
  --audit /tmp/plisio-audit.jsonl --accept-exact-derived-changes
```

Dit voegt nog geen automatische verwerking toe. Voor lange shelluitvoeringen
kan het bestaande CLI-proces los van de webterminal draaien; controleer altijd
de exclusieve audit, lock en complete-event voordat een poging wordt herhaald.

## Automatisch verwerken van nieuwe boekingen

`operations.automatic_debtor_routing` draait in de bestaande FastAPI-service,
zonder extra Render-service, cronjob of webhook. De controle start iedere vijf
minuten na afloop van de vorige controle; API-verwerking kan extra tijd kosten.
Dit is polling, geen onmiddellijke Exact-eventcallback.

De bestendige controlerij staat standaard uit. Een operator activeert eenmaal
met een expliciet UTC-starttijdstip van maximaal een uur geleden. Alleen daarna
in Exact aangemaakte verkoopboekingen op 100100 worden gevolgd, ongeacht hun
factuurdatum. Overlappende Modified-scans en een wachtrij voorkomen dat een
herstart of vertraagde Metorik-import een boeking overslaat. Ontbrekende orders
worden opnieuw alleen-lezen gezocht; andere betaalmethoden worden overgeslagen.
Een unieke live webshopkoppeling, originele bedragen en de bestaande volledige
open-postcontroles zijn verplicht. Deelbetalingen en onduidelijke posten gaan
naar beoordeling; zij worden niet automatisch gewijzigd.

De bestaande PostgreSQL-database bewaart controlestatus, wachtrij en audit in
`jnp_debtor_route_*`. Een databasebrede advisory lock en de lokale executorlock
voorkomen gelijktijdige runs, ook bij een deployment. Vóór iedere PUT wordt het
voorgenomen schrijfwerk bestendig opgeslagen. Zonder complete nacontrole blijft
de post onzeker en stopt de automatisering, ook na herstart. Geen automatische
herhaling van een onzekere PUT. De audit bevat voor/na-beelden van de geselecteerde
boeking; globale controlemomenten bewaren aantallen, saldi en SHA256's.

```sh
python -m operations.automatic_debtor_routing enable --since 2026-10-03T00:00:00+00:00
python -m operations.automatic_debtor_routing status
python -m operations.automatic_debtor_routing pause
```

Kies bij enable het actuele afgesproken startmoment, niet het voorbeeld hierboven.
`run-once` gebruikt dezelfde permanente instelling, locks en controles. Het is
geen mogelijkheid om een gepauzeerde of onzekere verwerking te forceren. Herstel
na een onzekere uitkomst vereist onderzoek van de audit en afzonderlijke actie.
De publieke healthcontrole toont alleen ingeschakeld/status/laatste scantijd,
geen boekingen, bedragen of geheimen. Bestaande matchingflags blijven ongewijzigd.

### Afzonderlijk beoordeelde orderkoppeling bij centverschillen

De operator kan één concreet entry-ID toevoegen met `--reviewed-line-link ID`.
Dit is uitsluitend voor een vooraf onderzochte afwijking van maximaal twee cent,
met dezelfde unieke orderidentiteit, datum, valuta, betaalmethode en openstatus.
Het live orderbewijs bevat dan bovendien productregels, korting en verzendwijze.
Alle artikelcodes, aantallen, nettobedragen en btw moeten exact aan unieke Exact-
regels aansluiten; de korting en verzendomschrijving moeten eveneens aansluiten.
De Exact-regels moeten het oorspronkelijke Exact-totaal en btw-totaal reproduceren.
De controlegegevens worden vóór uitvoering opnieuw bij Metorik gelezen. Dit
wijzigt geen enkel bedrag en verklaart de oorzaak van het centverschil niet.
Een centverschil alleen is nooit voldoende bewijs. De automatische verwerking
gebruikt deze optie niet en blijft afwijkende bedragen voor beoordeling apart zetten.

## Fibonatix

Dezelfde vaste route ondersteunt `wc_fibonatix → 109384` (bestaande debiteur
Verzameldebiteur Fibonatics), vanaf 100100 in administratie 3977752. De live
Exact-conditie moet `fi / wc_fibonatix / B` zijn. Ook deze route vereist uniek
Metorik-orderbewijs, werkelijke Exact-restbedragen en alle bovenstaande controles.
Een PP-verkoopconditie blijft PP. Andere betaalmethoden, voldane en deelbetaalde
posten worden niet naar 109384 omgezet.

Voor bestaande open posten: gebruik het manifest en `--payment-method wc_fibonatix`.
Werk in kleine gecontroleerde batches; een Render-webshell kan een langlopend
proces beëindigen. Archiveer iedere complete audit in de bestaande database.
De bestaande automatische verwerking neemt de nieuwe route mee zonder reset van
controlestatus, cursor, wachtrij of audit. Controleer bij de ingebruikname ook de
actuele open posten die eerder als andere betaalmethode werden overgeslagen.
Er worden geen nieuwe infrastructuur, financiële velden of algemene instellingen
toegevoegd. Alle bestaande bestemmingen worden vóór iedere automatische scan
opnieuw live gecontroleerd.
