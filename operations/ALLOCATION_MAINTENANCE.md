# Banktoewijzing en regelonderhoud — 4 oktober 2026

Opdracht van de operator: onderzoek open banktransacties, maak onderbouwde
Exact-toewijzingsregels en pas die vervolgens via de knop **Automatically** toe.
PSP-uitbetalingen blijven buiten deze route: de operator wil daarvoor later eigen
bankdagboeken en kruispostrekeningen. De bestaande debiteurenroutering blijft staan.

## Uitgevoerde controle

De bestaande Render-service leest ieder uur alle bankregels op 1360 en de actuele
openstaande verkoopposten, inclusief alle paginapagina's. Leveranciers en geboekte
uitgaande betaalhistorie worden dagelijks ververst. Er is een API-reserve van 200,
geen sleutelwissel en geen bank-, memoriaal- of MatchSets-write.

Nieuwe regels zijn alleen toegestaan voor:

- Een expliciete btw-teruggaaf van de Belastingdienst: relatie 1 en balansrekening
  1770, met de volledige omschrijving of de unieke BOSCI-code als herkenning.
  Rente, boete, verrekening, tegenstrijdige belastingsoorten of ongeldige
  betalingskenmerken blijven ter beoordeling.
- Een unieke bestaande leveranciersnaam, met minstens twee eerder geboekte
  betalingen op die relatie. De regel gebruikt de volledige bankomschrijving en
  alleen de relatie; kostenrekening en btw worden niet uit de naam afgeleid.
- Eén bankontvangst voor één unieke openstaande BACS-factuur op 109372, met exact
  hetzelfde bedrag en EUR-valuta, bevestigd in Metorik zonder terugbetaling. De
  bankregel en factuur worden direct voor het maken van de regel opnieuw gelezen.

Per ronde worden maximaal 25 nieuwe regels gemaakt. Een duurzame aanmaakintentie
wordt eerst vastgelegd, waarna de regel uit Exact wordt teruggelezen. Onzekere
POSTs worden niet blind herhaald. Dit bevestigt uitsluitend dat de **regel** bestaat,
niet dat de banktransactie al is toegewezen of afgeletterd.

## Dagelijkse opschoning

- Exact gelijke dubbele regels: bewaar één regel, inclusief gelijke relatie,
  grootboek, IBAN, tekst, kostenplaats, kostendrager en btw-code. Lees vlak voor
  verwijdering opnieuw; verwijder niets als de regel of bewaarde kopie gewijzigd is.
- Tijdelijke BOSCI-regels: pas na 90 dagen, een uniek gevonden bankbedrag op
  debiteurenrekening 1100/relatie 109372 en een factuur die niet meer openstaat.
- Bewaar de verwijderde payload, reden en Exact-ID in de controlehistorie.
- Vaste leveranciers-IBAN-regels blijven bestaan zolang ze niet identiek dubbel zijn.
- Belastingregels verlopen **niet** na één betaling of uitsluitend door ouderdom.
  Vpb kan in termijnen worden betaald. Voor btw hanteert deze administratie een
  betaling van het volledige aangiftebedrag. Een betaalomschrijving bewijst niet
  dat het totaal van een aangifte of aanslag voldaan is. Zonder dat bewijs blijft
  de belastingregel behouden; duplicaten kunnen wel worden samengevoegd.

De bestaande Woo-verwerking en belastingverwerking krijgen tijdens het onderhoud
geen gelijktijdige mutatie aan hun regels (gedeelde advisory locks). De nieuwe
onderhoudstaak heeft een eigen pauze en respecteert ook de belastingagentpauze.

## Automatically

De bedoelde knop staat rechtsboven in **Statements | To be completed**. Het
actuele openbare REST-resourceoverzicht bevat AllocationRule en bankimport, maar
geen gedocumenteerde actie om deze knop op bestaande afschriften te starten.
BankEntryLines biedt evenmin een PUT. Bankbestanden worden niet opnieuw geïmporteerd
als vervanging voor deze knop.

De API maakt de regels gereed. De service meldt `refresh_required`; dat is geen
bewijs dat Automatically is uitgevoerd. Bedien die stap via een aangemelde
Exact-browsersessie, controleer filters en administratie 3977752, en vergelijk
na afloop de resterende 1360-regels. Browsertoegang heeft een eigen aanmelding en
is niet inbegrepen in de OAuth-API-koppeling. Een permanente browseruitvoerder is
in deze service niet geconfigureerd.

Operationele status: `/api/allocation-maintenance/status` en `/health`.
Het volledige verslag staat achter de bestaande Allocation-operatorsessie op
`/api/allocation-maintenance/report`. Er is geen nieuw openbaar financieel endpoint.

Bronnen en verificatie:
- https://start.exactonline.nl/docs/HlpRestAPIResources.aspx
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=CashflowAllocationRule
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialTransactionBankEntryLines
- Knop en schermnaam bevestigd in de door de operator aangeleverde screenshot.
