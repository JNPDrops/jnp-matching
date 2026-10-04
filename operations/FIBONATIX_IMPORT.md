# Eenmalige Fibonatix-importcontroller

De expirerend beveiligde controller verwerkt uitsluitend de eerder goedgekeurde,
vaste importbatch. Administratie, bankboek, payloadhash en controles staan in de
controller. Brongegevens, bedragen, individuele order-/factuurreferenties en
browserartefacten horen in het private batchdossier, niet in deze documentatie.

Een Bearer-token uit Render Environment beveiligt de routes. Het token hoort
uitsluitend in de bestaande beveiligde omgeving en de private operatorclient.
Geen tokens, bronregels of browsercookies in Git, healthstatus of logs.

## Beschikbare verwerking

1. `POST /prepare`: valideer het vaste bronbestand en bewaar het onveranderd.
2. `POST /preflight`: lees alle Exact-pagina's en controleer op bestaande
   transacties en bezette boekingsnummers; bewaar de uitgangstoestand.
3. `POST /inspect`: lees de bankafschriftpagina met de bestaande Exact-login.
4. `POST /import`: herhaal de controles, claim duurzaam precies één upload en
   verifieer iedere geïmporteerde regel. Herhaal nooit een onzekere upload.
5. `POST /reconcile`: lees uitsluitend terug; verwijder nooit een write-claim.
6. `POST /audit_order_matches`: inventariseer afwijkingen tussen bronorder en
   toegewezen referentie over de hele geïmporteerde periode. Bewaar de volledige
   private inventarisatie in `artifact/order_match_audit`.

`GET /status` en `GET /artifact/{name}` vereisen hetzelfde token. De gegevens
blijven in de bestaande Postgres-database. Een database-lock voorkomt gelijktijdige
controllerruns. Een deployment of herstart voert geen financiële actie uit.

## Gewijzigde afletterregel

De instructie van 4 oktober 2026, 21:45 CEST vervangt het eerdere akkoord voor
afletteren op verzameldebiteuren-saldo. Iedere betaling moet aan de eigen order
en factuur worden gekoppeld. De controller blokkeert de oude Automatically-actie
en de historische afwikkeling met een betaling van een andere order. De directe
MatchSets-schakelaar blijft uitgeschakeld.

De bestaande import is vóór automatisch afletteren gecontroleerd. Exact kan bij
afletteren de toegewezen referentie wijzigen terwijl bronomschrijving en bedrag
gelijk blijven. De strikte importvergelijking meldt zo'n referentiewijziging;
dit signaleert onderzoek naar de koppeling, niet vanzelf een verkeerd importbedrag.

Een afwijkende referentie is een kandidaat voor herstel. Controleer de echte
aflettering en de bijbehorende factuur voordat die wordt losgemaakt. Bewaar
reeds goedgekeurde betalingsverschillen bij de eigen order zonder dubbele
afboeking. Er is geen algemene automatische afboekgrens afgesproken.

De inventarisatie corrigeert nog geen bestaande afletteringen. Een gerichte
herstelfunctie en de dagelijkse verwerking moeten de eigen order en factuur
vóór iedere match controleren en de werkelijke uitkomst daarna teruglezen.

De dashboardvereisten en volledige beslisregels staan in
[DASHBOARD_EXCEPTIONS.md](DASHBOARD_EXCEPTIONS.md).
