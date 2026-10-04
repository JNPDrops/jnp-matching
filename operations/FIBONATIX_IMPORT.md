# Eenmalige Fibonatix-import t/m 2 oktober 2026

De beveiligde routes onder `/ops/fibonatix-20261002` zijn alleen actief met een
minstens 40 tekens lange `EXACT_IMPORT_CONTROL_TOKEN` en vervallen op 5 oktober
2026 om 18:00 UTC. Het token hoort uitsluitend in Render Environment en een
privé operatorclient. Geen tokens, XML-bronregels of browsercookies in Git,
gezondheidsstatus of logs.

Deze controller accepteert uitsluitend de door de gebruiker goedgekeurde batch
`FIBO-20260922-20261002`, met SHA-256
`18574a6b7fd6cad2fc9144c1d8c0a1c279830d92739cd70f5410db89b9de94f2`.
Het bestand heeft 845 transacties in 11 dagboekingen: 842 ontvangsten van
€85.681,83 en 3 refunds van €180,97, netto €85.500,86. Administratie 3977752,
dagboek 26, bankgrootboek 1316, debiteur 100100 op 1100; refunds volgens het
gevalideerde bronbestand op 1350 zonder relatie. Geen fees of uitbetalingen
toevoegen en geen eerder geïmporteerde eerste-weekregels wijzigen.

De operator uploadt het ongewijzigde XML via `POST /prepare`. Daarna:

1. `POST /preflight`: alle pagina's van Exact dagboek 26 lezen, transacties en
   vrije boekingsnummers controleren, oorspronkelijke toestand bewaren.
2. `POST /inspect`: met de bestaande Exact-loginfunctie de afschriftpagina
   voor Fibonatix openen; alleen lezen. De beveiligde `artifact/ui` bevat de
   zichtbare pagina voor het afronden van de gecontroleerde verwerking.
3. `POST /import`: opnieuw controleren, duurzaam één uploadpoging claimen,
   GLTransactions één keer uploaden via de bestaande Exact XML API, en elke
   transactie teruglezen. Een timeout/401/andere onduidelijke uitkomst wordt
   nooit door een tweede upload gevolgd.
4. `POST /reconcile`: uitsluitend teruglezen en controleren. Gebruik dit na
   een herstart of twijfel over de uitkomst; verwijder geen write-claim.

`GET /status` en `GET /artifact/{name}` vereisen hetzelfde Bearer-token. De
artefacten en het XML blijven in de bestaande Postgres-database. Een
database-lock voorkomt gelijktijdige runs van deze controller. Er start geen
financiële actie op deployment of herstart. Bestaande MatchSets-schakelaars,
toewijzingsregels, dagelijkse agents en de handmatige deployinstelling blijven
ongewijzigd.

Deze eerste controller bevat nog geen Automatically-actie. Die wordt pas na
inspectie van de actuele Fibonatix-afschriftpagina aangesloten. De uiteindelijke
aflettering mag geen verschillen afboeken en moet resterende open posten
rapporteren. De dagelijkse Paragon-import is een afzonderlijke vervolgstap.
