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

`POST /automatic` is eenmaal uitgevoerd na verificatie van alle 845 regels.
Exact heeft daarbij 77 orderreferenties vervangen bij aflettering op gelijke
bedragen. De oorspronkelijke order, Woo-ID en transactiereferentie blijven in
omschrijving/notitie staan. Jasper accepteert deze aflettering binnen 100100
als het saldo klopt en betaalde orders gesloten zijn (4 oktober, 21:13 CEST).
Daarom geen massale herstelactie uitsluitend vanwege een andere YourRef.
De strikte importvergelijking blijft deze referentiewijzigingen rapporteren;
dat is na Automatically geen bewijs van een verkeerd geïmporteerd bedrag.

`POST /settle_48189` is begrensd tot factuur 26722396 / TD48189 (€85,00) en
de bestaande ontvangst j8hJ8KLB (€90,99). Jasper heeft de €5,99 expliciet als
betalingsverschil goedgekeurd op 4 oktober om 21:19 CEST. De controller controleert
de actuele bedragen en identiteit vóór éénmalig opslaan, en leest daarna de
gesloten posten en de €5,99 op 9920 terug. Een onzekere uitkomst leidt tot
teruglezen, nooit tot nogmaals opslaan. Dit stelt geen algemene afboekgrens in.

Beslisregels en vereisten voor de dashboardwerklijst staan in
[DASHBOARD_EXCEPTIONS.md](DASHBOARD_EXCEPTIONS.md). De dagelijkse Paragon-import
en de dashboardimplementatie zijn afzonderlijke vervolgstappen.
