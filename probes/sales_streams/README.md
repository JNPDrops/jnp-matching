# Proef: debiteur per betaal-/afrekenstroom

Dit is een zelfstandig, offline hulpmiddel voor de bestaande JNP-repository.
Geen netwerkclient, Exact-import, webroute, startup-hook of deployment.
De productiecode en de bestaande Xcore-koppeling worden niet aangeroepen.

## Uitvoeren

Vanaf de repository-root (Python 3, geen extra packages):

```sh
python probes/sales_streams/preview.py --orders /private/orders.json --mapping /private/mapping.json
python -m unittest discover -s probes/sales_streams -v
```

De uitvoer verschijnt als JSON op stdout. Bewaar echte exports, mappings en
rapporten uitsluitend privé; deze repository is openbaar. Gebruik geen tokens
of persoonsgegevens in commando's, commits of PR's.

De orderinput is een JSON-array met per order: `id` (integer), `number`
(numerieke string), `payment_method`, `status`, `currency`, `total` (decimale
string) en `refunds` (array met de refund-samenvattingen). Extra velden zoals
billing worden niet overgenomen in de uitvoer. Dit is geen CSV-importer.
Gebruik de werkelijke ordernummers, niet automatisch de interne WooCommerce-ID.

`mapping.example.json` bevat uitsluitend de drie in het dashboard waargenomen
codes. Hun afrekenstromen en nieuwe debiteuren zijn NIET vastgesteld. De twee
andere door de gebruiker genoemde betaalmethoden ontbreken nog. Vul een private
mapping pas na controle in, met `stream`, `debtor_code` en `confirmed: true`.
Meerdere betaalcodes mogen dezelfde afrekenstroom en debiteur krijgen.
Er worden geen debiteuren aangemaakt of gevalideerd; ook een ingevulde code blijft
een voorstel, geen bevestigd Exact-account/GUID.

## Betekenis van de uitkomst

- `ROUTING_PROPOSAL`: de aangeleverde gegevens voldoen aan de beperkte routingchecks.
- `REVIEW`: bijvoorbeeld onbekende betaalcode, onbesliste mapping, dubbele ID/nummer,
  ontbrekende refundinformatie, refund, andere valuta of orderstatus.
- `exact_write_allowed` is altijd false; `exact_comparison` blijft NOT_PERFORMED.
- Er is geen fallback naar 100100 of naar een gegokte betaalstroom.
- Een refund leidt tot handmatige beoordeling; er wordt geen creditbedrag of
  creditboeking afgeleid uit een samenvatting.

De proef bewijst geen btw-correctheid, volledige orderdekking, juiste settlement,
correcte verkoopboeking of aflettering. Herhaald uitvoeren schrijft niets extern.
Er is nog geen productie-idempotentieregister en geen live WooCommerce-uitlezing.

## Demonstratie

```sh
python probes/sales_streams/preview.py --orders probes/sales_streams/orders.synthetic.json --mapping probes/sales_streams/mapping.example.json
```

De demonstratie gebruikt verzonnen orders en bedragen. Alle vier orders moeten
REVIEW blijven: de drie bekende codes hebben bewust nog geen bestemming en de
vierde code is onbekend. Dit is geen proef op klantdata.

## Volgende bewijspoort

1. Lees een afgebakende echte orderexport of maak veilige WooCommerce-leestoegang
   beschikbaar; leg bron, periode, selectie en volledigheid afzonderlijk vast.
2. Inventariseer alle vijf betaalcodes en de echte settlementrelaties.
3. Bevestig gewenste Exact-debiteuren, codes en GUID's (alleen-lezen).
4. Vergelijk vervolgens verkoop- en creditbedragen, btw, korting, verzendkosten en
   orderreferenties met de werkelijke Xcore-boekingen. Deze vergelijking is nog
   niet geïmplementeerd in deze eerste routingproef.
5. Pas na afzonderlijke beoordeling een eventuele productiekoppeling ontwerpen.
   Geen tweede schrijver naast Xcore; geen historische herboeking of bankherimport.

## Kosten

Het gedeelde accountoverzicht toont EUR 18 per maand (EUR 216 per jaar bij gelijk
blijvend tarief). Dit is hoogstens vermijdbare abonnementslast bij volledige
vervanging, geen bewezen nettobesparing. Onderhoud, hosting, API-wijzigingen,
monitoring en herstelwerk tellen mee. Voor deze offline proef is geen nieuwe
infrastructuur of betaald abonnement aangemaakt.
