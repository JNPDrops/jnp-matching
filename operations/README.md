# Debiteurenomzetting

## Actieve agents: alleen de debiteur wijzigen (3 oktober 2026)

Op expliciet verzoek gebruiken de automatische router en Fibonatix-inhaalqueue
`customer_only_routing.py`. De goedgekeurde bestemmingen blijven bacs → 109372,
plisio → 109377 en wc_fibonatix → 109384, vanaf 100100 in administratie 3977752.

- De inhaalqueue hergebruikt de reeds opgeslagen orderkoppeling en betaalmethode.
- Nieuwe orders worden eenmaal per groep bij Metorik opgezocht; geen herhaalde bewijsplannen.
- Bestaande debiteur-GUIDs worden eenmaal per proces opgezocht en daarna hergebruikt.
- Per post: één beperkte kopregelread, één gerichte open-postread en één PUT met
  uitsluitend `Customer`. Geen volledige snapshots, boekingsregels, btw-,
  cashflow-, totale balans- of na-controles.
- De open-postread bewaakt alleen de oorspronkelijke selectie: de bestaande
  verkoopboeking moet nog op 100100 staan en een positief restbedrag hebben.
- Een geslaagde Exact-HTTP-respons wordt als `applied` opgeslagen. Dat is geen
  claim dat de bedragen of saldi opnieuw zijn gecontroleerd. Onzekere PUT-uitkomsten
  worden niet automatisch herhaald; de bestaande pauzeschakelaar en audit blijven.
- Eén batch omvat maximaal 10 posten (normaal 30 Exact-aanroepen). De reservering
  is teruggebracht van 1000 naar 100 aanroepen; de bestaande snelheidslimiet blijft.

De onderstaande eenmalige CLI behoudt zijn oorspronkelijke uitgebreide controles;
de actieve agents gebruiken deze oude uitvoerder niet meer.

## Eenmalige bacs-debiteurenomzetting

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
met een expliciet UTC-starttijdstip van maximaal een uur geleden. Daarna worden
nieuwe of gewijzigde verkoopboekingen op 100100 gevolgd, ongeacht hun aanmaak-
of factuurdatum. Een import mag een andere omschrijving hebben: de TD-referentie
moet alsnog bewezen aan een webshoporder gekoppeld worden. Overlappende Modified-scans en een wachtrij voorkomen dat een
herstart of vertraagde Metorik-import een boeking overslaat. Ontbrekende orders
worden opnieuw alleen-lezen gezocht; andere betaalmethoden worden overgeslagen.
De opgezochte betaalmethode en unieke orderreferentie bepalen de vaste route.
De minimale uitvoerder hierboven controleert alleen de boekingsidentiteit,
brondebiteur, bewerkbare verkoopstatus en een positieve openstaande post.

De bestaande PostgreSQL-database bewaart controlestatus, wachtrij en audit in
`jnp_debtor_route_*`. Een databasebrede advisory lock en de lokale executorlock
voorkomen gelijktijdige runs, ook bij een deployment. Vóór iedere PUT wordt het
voorgenomen schrijfwerk bestendig opgeslagen. Zonder bevestigde HTTP-respons blijft
de post onzeker en wordt uitsluitend die post apart gehouden, ook na herstart.
De overige posten en nieuwe imports blijven doorlopen. Geen automatische
herhaling van een onzekere PUT. De audit bewaart bron, doel, orderkoppeling en
schrijfresultaat; er worden geen saldi of volledige voor/na-beelden opgehaald.

```sh
python -m operations.automatic_debtor_routing enable --since 2026-10-03T00:00:00+00:00
python -m operations.automatic_debtor_routing status
python -m operations.automatic_debtor_routing pause
```

Kies bij enable het actuele afgesproken startmoment, niet het voorbeeld hierboven.
`run-once` gebruikt dezelfde permanente instelling, locks en controles. Het is
geen mogelijkheid om een handmatige pauze te omzeilen of een onzekere post opnieuw
te schrijven. Herstel van zo'n afzonderlijke post vereist een aparte actie.
De publieke healthcontrole toont alleen ingeschakeld/status/laatste scantijd,
geen boekingen, bedragen of geheimen. Bestaande matchingflags blijven ongewijzigd.

Op 3 oktober 2026 gaf de gebruiker opdracht de doorlopende verwerking te herstellen.
`routing_runtime.recover_once` hervat daarom eenmalig de reeds geïnitialiseerde
router, bewaart de bestaande scanpositie en laat alle poststatussen intact.
`continuous_routing_recovered_at` legt dit vast in de bestaande controlerij;
een latere handmatige pauze blijft gerespecteerd, ook na een deployment.
De vorige stopredencategorie en wachtrij-aantallen verschijnen uitsluitend in de
Render-servicelogs. Er worden geen geheimen, saldi of API-responsebodies gelogd.

Een expliciete HTTP 429/401/403-afwijzing laat de post wachtend staan en beëindigt
de huidige ronde; een HTTP 400 e.d. zet alleen die post op `review`. Bij een
onzekere schrijfuitkomst (transport/5xx) blijft de post `uncertain` zonder herhaling.
GET-transportfouten worden in een volgende ronde opnieuw geprobeerd. De agent
blijft ingeschakeld en hervat na voldoende API-budget; een bekende daglimiet-reset
wordt gerespecteerd zonder iedere vijf minuten de uitgeputte API aan te roepen.

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
gebruikt deze CLI-optie niet; haar minimale route vergelijkt geen orderbedragen.

## Fibonatix

Dezelfde vaste route ondersteunt `wc_fibonatix → 109384` (bestaande debiteur
Verzameldebiteur Fibonatics), vanaf 100100 in administratie 3977752. De handmatige
CLI controleert de Exact-conditie `fi / wc_fibonatix / B`. De actieve agent gebruikt
de reeds vastgestelde Metorik-betaalmethode en schrijft uitsluitend Customer.
Andere betaalmethoden en volledig voldane posten worden niet naar 109384 omgezet.

Voor bestaande open posten: gebruik het manifest en `--payment-method wc_fibonatix`.
Werk in kleine gecontroleerde batches; een Render-webshell kan een langlopend
proces beëindigen. Archiveer iedere complete audit in de bestaande database.
De bestaande automatische verwerking neemt de nieuwe route mee zonder reset van
controlestatus, cursor, wachtrij of audit. Controleer bij de ingebruikname ook de
actuele open posten die eerder als andere betaalmethode werden overgeslagen.
Er worden geen nieuwe infrastructuur, financiële velden of algemene instellingen
toegevoegd. Bestaande bestemmings-GUIDs worden eenmaal per serviceproces opgezocht.

De Exact- en Metorik-clients hergebruiken dezelfde geverifieerde TLS-certificaatstore.
Een health-only proef op Render liet bij 25 nieuwe stores ongeveer 24 MiB groei
zien, tegenover circa 0,125 MiB met hergebruik. De CA-bundel, hostnamecontrole,
timeouts, geen redirects en geen automatische PUT-herhaling blijven gelijk.
Zie https://www.python-httpx.org/advanced/ssl/ voor de equivalente SSL-context.

### Hervatbare Fibonatix-inhaalronde

Een grote bestaande populatie wordt alleen uit een expliciet, onveranderlijk
opgeslagen leesonderzoek geïmporteerd. `backfill_debtor_routing seed
--discovery-sha SHA` controleert administratie, bron, doel, hash, leeftijd, unieke
entry/order-koppelingen en de bestaande bestemming. Het maakt geen Exact-mutatie.
Alleen dat cohort komt in `jnp_debtor_route_backfill` in de bestaande database.

De bestaande service verwerkt eerst nieuwe boekingen en daarna maximaal tien
historische posten. Iedere batch hergebruikt de opgeslagen cohortselectie en
gebruikt de minimale Customer-only uitvoerder zonder herhaalde order- of saldocontrole.
Voldane en onduidelijke posten worden afzonderlijk gerapporteerd.
Selectie en auditgebeurtenissen worden vóór de mutatie bestendig gearchiveerd.
Een onzekere post wordt apart gehouden; andere posten blijven beschikbaar voor
verwerking. Schrijfintenties worden per post bijgehouden, zodat een latere leesfout
niet door een eerdere geslaagde schrijfopdracht als onzekere mutatie wordt gezien.

De actuele Exact-daglimiet wordt uit de responseheaders gelezen. De inhaalronde
start met minstens 130 aanvragen beschikbaar: 30 voor de batch en 100 reserve.
Ook voor iedere post en PUT wordt het laatst ontvangen budget lokaal nagekeken.
Bij minder ruimte wacht de inhaalronde; de
bestaande vijfminutencyclus hervat vanzelf zodra er weer voldoende ruimte is.
Er wordt geen Exact-limiet omzeild. Het opzoeken van alle bestemmingen gebruikt
één gedeelde leesaanvraag per serviceproces.

`python -m operations.backfill_debtor_routing status` toont de cohortvoortgang.
Iedere succesvolle PUT telt direct na het permanente `customer_applied`-auditmoment
als toegepast. `applied` betekent HTTP-bevestigd, zonder nacontrole van boeking of
balans. Oude `verified`-resultaten blijven behouden. Nieuwe posten en lopende
inhaalbatches delen dezelfde locks.
