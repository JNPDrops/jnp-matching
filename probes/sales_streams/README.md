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

## Aanvulling: WooCommerce lezen vanuit de bestaande Render-service

`woo_read.py` is een afzonderlijke GET-only client naast de offline proef hierboven.
Hij wordt niet bij startup uitgevoerd en voegt geen openbare route toe. Hij gebruikt
het al bestaande projectpakket `httpx==0.28.1`.

Stel uitsluitend op de bestaande `jnp-matching` service in:

- `WOO_BASE_URL`: de canonieke HTTPS-webshop-URL (eventuele WordPress-subdirectory,
  geen `/wp-admin` of `/wp-json`, geen queryparameters).
- `WOO_CONSUMER_KEY`: afzonderlijke WooCommerce-consumersleutel.
- `WOO_CONSUMER_SECRET`: bijbehorend geheim.

Maak de sleutel via WooCommerce > Instellingen > Geavanceerd > REST API aan,
met beschrijving `JNP Matching leesproef`, toegang **Read/Lezen** en een gebruiker
met de noodzakelijke orderleestoegang. Gebruik niet de sleutel van Xcore.
De scope moet in WooCommerce worden gecontroleerd: een GET-test bewijst niet
of een sleutel ook schrijfrechten heeft. Voer waarden rechtstreeks in Render in,
nooit in GitHub, screenshots of chat. Beperk de sleutel tot deze service.

Bewaar eerst met Save only. Pas na gecontroleerd samenvoegen/deployen van deze
code is de client op Render beschikbaar. In de Render Shell, vanuit de repo-root:

```sh
python probes/sales_streams/woo_read.py --check
```

Dit doet één GET op orders met alleen ID als aangevraagd veld en toont uitsluitend
verbindingsstatus, geen orderdetails. Daarna kan een expliciet gekozen venster:

```sh
python probes/sales_streams/woo_read.py --after 2026-09-01T00:00:00 --before 2026-09-02T00:00:00 --mapping probes/sales_streams/mapping.example.json
```

Het voorbeeldvenster is geen opdracht voor een live uitlezing. Datumfilters volgen
WooCommerce-orderaanmaak, niet bankdatum. Controleer de tijdzone en grenssemantiek
van de winkel voordat perioden boekhoudkundig worden vergeleken.

HTTPS met certificaatcontrole en Basic Auth in de Authorization-header; sleutels
komen niet in URL's. Redirects worden geweigerd, foutbodies niet gelogd. De bestemming
is uitsluitend serverconfiguratie, nooit een publiek requestparameter. Sleutels niet
in de dev-agent zetten. Er is geen aanvraag naar Exact en geen financiële schrijfcode.

Pagina-aantallen, recordaantallen en unieke IDs worden gecontroleerd. Bij meer dan
20 pagina's standaard, gewijzigde aantallen of incomplete respons wordt geen
rapport uitgegeven. Dit is geen transactionele snapshot; orders kunnen tijdens de
uitlezing veranderen. Refunddetails en volledige boekingsvergelijking volgen later.

Officiële referenties:
- https://woocommerce.com/document/woocommerce-rest-api/
- https://woocommerce.github.io/woocommerce-rest-api-docs/

## Metorik als alternatieve leesbron

`metorik_read.py` gebruikt alleen de vaste HTTPS-host app.metorik.com en GET op
`/api/v1/store` en `/api/v1/store/orders`. De sleutel komt uit `METORIK_API_KEY`
in de bestaande Render-service, met Metorik-scope Reports & Data. Geen Engage-scope.

```sh
python probes/sales_streams/metorik_read.py --check
```

Deze controle leest uitsluitend winkelmetadata. Hij toont winkelnaam, platform,
valuta en tijdzone voor identiteitscontrole; geen orders, geheimen of headers.
Een succesvolle check bewijst nog geen volledige orderuitlezing of synchronisatie.
Pas na controle van de winkelnaam kan een beperkte steekproef worden gelezen:

```sh
python probes/sales_streams/metorik_read.py --expect-store 'EXACTE NAAM UIT CHECK' --sample-limit 50 --mapping probes/sales_streams/mapping.example.json
```

De steekproef omvat de laatste orders op aanmaakdatum, maximaal 500. Paginering en
unieke bron-ID's worden gecontroleerd; gewijzigde records tijdens uitlezen blijven
mogelijk. Persoonsvelden uit het API-antwoord worden direct weggefilterd. De API
ondersteunt hier geen gedocumenteerde veldselectie; volledige records worden wel
ontvangen in het procesgeheugen, maar niet opgeslagen of gelogd.

Alle uitkomsten blijven REVIEW: Metorik-order-ID versus WooCommerce-ID, oorspronkelijke
valuta/bedragsemantiek en refunddetails zijn nog niet onafhankelijk gecontroleerd.
Een niet-nul total_refunds is uitsluitend een beoordelingssignaal, nooit een
nagebouwde creditboeking. Een nul is geen bewijs van complete refundhistorie.
Geen Exact-mutaties, nieuwe webroute, startup-hook of achtergrondtaak.

Officiële bron (geraadpleegd 2 oktober 2026): https://metorik.dev/

## Vervolg: actuele openstaande posten in Exact per betaalstroom

Op verzoek van de gebruiker wordt de onderzoeksvraag uitgebreid naar de thans
openstaande debiteurenposten. Dit is nog niet geïmplementeerd of live uitgevoerd.
De basispopulatie moet een actuele, volledig gepagineerde Exact-uitlezing zijn
voor administratie 3977752, met datum/tijd van uitlezing en daadwerkelijk
restbedrag per unieke post. Gebruik niet het WooCommerce-ordertotaal als restsaldo.

Koppel op bewezen orderreferentie/identiteit; behoud Exact-post-ID, debiteurcode,
valuta, oorspronkelijke bedrag, openstaand bedrag en orderreferentie. Markeer
ontbrekende/meervoudige referenties, deelbetalingen, credits en conflicterende
betaalmethoden voor beoordeling. Posten buiten de webshop mogen niet stilzwijgend
onder een webshop-betaalstroom vallen. Totaliseer restbedragen per valuta en
betaalstroom, inclusief onbekend, en reconcilieer met de bronpopulatie.

Een indelingsrapport is geen wijziging van een geboekte debiteur. Vóór aanpassing
binnen Exact moeten de nieuwe debiteuren, ondersteunde wijzigingsroute, gevolgen
voor bestaande afletteringen en de concrete betrokken posten worden vastgesteld.
De bestaande operationele bankroute en Xcore blijven actief. Geen automatische
herboeking, aflettering, undo, verwijdering of herimport in deze leesproef.
