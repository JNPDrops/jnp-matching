# Banktoewijzing en regelonderhoud — 4 oktober 2026

Opdracht van de operator: onderzoek open banktransacties, maak onderbouwde
Exact-toewijzingsregels en pas die vervolgens via de knop **Automatically** toe.
PSP-uitbetalingen blijven buiten deze route: de operator wil daarvoor later eigen
bankdagboeken en kruispostrekeningen. De bestaande debiteurenroutering blijft staan.

## Uitgevoerde controle

De bestaande Render-service leest ieder uur alle bankregels op 1360, open
debiteuren- en crediteurenbetalingen uit alle bankboeken (ook PSP-bankboeken),
bankregels op vraagposten 2000 en de actuele
openstaande verkoop- en inkoopposten, inclusief alle paginapagina's. Leveranciers en geboekte
uitgaande betaalhistorie worden dagelijks ververst. Er is een API-reserve van 200,
geen sleutelwissel en geen bank-, memoriaal- of MatchSets-write.

Nieuwe regels zijn alleen toegestaan voor:

- Een expliciete btw-teruggaaf van de Belastingdienst: uitsluitend balansrekening
  1770, zonder relatie, met de volledige bewezen omschrijving als herkenning.
  Rente, boete, verrekening, tegenstrijdige belastingsoorten of ongeldige
  betalingskenmerken blijven ter beoordeling.
- Een unieke bestaande leveranciersnaam, met minstens twee eerder geboekte
  betalingen op die relatie. De regel gebruikt de volledige bankomschrijving en
  alleen de relatie; kostenrekening en btw worden niet uit de naam afgeleid.
- Eén bankontvangst voor één unieke openstaande BACS-factuur op 109372, met exact
  hetzelfde bedrag en EUR-valuta, bevestigd in Metorik zonder terugbetaling. De
  bankregel en factuur worden direct voor het maken van de regel opnieuw gelezen.

## Beoordeling van al toegewezen betalingen (v1.12)

- Een actuele bankpost in ReceivablesList/PayablesList is het bewijs dat een
  betaling nog openstaat. Een 1100/1400-boeking op zichzelf bewijst dat niet.
  De live Cashflow API liet geïmporteerde bankbetalingen weg; gebruik daarom de
  open-postenlijsten. Koppel uitsluitend een unieke bankregel op dagboek,
  boekingsnummer, relatie, datum, valuta, richting en bedrag. Voor een
  deelaflettering moet ook het oorspronkelijke bankbedrag onderbouwd zijn.
  Onzekere identiteiten blijven in een aparte controlelijst. Dit levert alleen
  kandidaten: uitvoering vereist nog actuele transaction-line-identiteit.
  Haal bankheaders alleen op voor de EntryID's van de huidige kandidaten.
- Gebruik het resterende bankbedrag en het resterende factuurbedrag. Noem een
  openstaand saldo nooit het oorspronkelijke factuurbedrag.
- Gebruik alleen verkoop-/inkoopdagboeken als factuurbron, zodat een andere
  bankbetaling niet als ontbrekende factuur wordt voorgesteld.
- Betaling vóór factuurdatum is mogelijk bij facturatie na verzending. Controleer
  de besteldatum in Europe/Amsterdam; een betaling vóór de bestelling vraagt
  onderzoek. Verifieer BACS-betaalmethode, bedrag, valuta, status en refunds.
- Bij gelijke betaling en webshopordertotaal maar afwijkend factuursaldo: controleer
  import, credits en eerdere afletteringen. Boek het verschil niet automatisch af.
- Leveranciers: geef een matchkandidaat alleen bij dezelfde relatie, valuta,
  tegengestelde richting, eenduidige factuurreferentie en exact openstaand bedrag.
  Alleen hetzelfde bedrag is een zoekkandidaat, geen bewezen match.
- Herken ook `cancelation`, `cancellation`, `overpayment` en dubbele-betalingsrefunds.
  Die vragen de oorspronkelijke ontvangst of creditnota, geen kostenregel.
- Herken PSP-bankboeken via dagboekmetadata, ook als er alleen `Order 43960` staat.
  Een uitgaande `PAYPAL *merchant`-kaartbetaling is geen PSP-uitbetaling.
- Signaleer mogelijke dubbele regels op eigen bankboek, datum, valuta, bedrag en
  bronkenmerk/tegenpartij. Verwijder nooit op die gelijkenis. Herbeoordeling gebeurt
  uit de actuele Exact-data; elders verwijderde imports verdwijnen uit de lijst.

De bestaande regelschrijver blijft actief voor bewezen 1360-toewijzingen. De
uitbreiding schrijft geen bankboekingen of MatchSets, activeert geen oude
memoriaalroute en beweert niet dat een kandidaat al afgeletterd is. Rechtstreeks
afletteren en Automatically vereisen nog de Exact-uitvoerstap en resultaatcontrole.
Iedere regel krijgt `next_action`, `reason` en `retry_when` en wordt ieder uur
herbeoordeeld. `/allocation/review` toont dit achter dezelfde operatorsessie als
het JSON-verslag; de openbare status bevat alleen aantallen en processtatus.

## Volledige betaalwerklijst (maintenance-v5)

De opdracht van 4 oktober is om iedere niet-matchbare open betaling met een
concrete afhandelinstructie zichtbaar te houden. Het dashboard heeft één lijst
met zoekfunctie en filters voor afhandeling, reden en dagboek. Ook een niet
eenduidig geïdentificeerde open bankpost blijft daarin staan, met bedrag,
datum, bronreferentie, relatie en benodigde controle. De startpagina linkt naar
deze beveiligde werklijst. De bestaande Allocation-operatorsessie blijft vereist.

PSP-ontvangsten in hun eigen bankboek worden nu alleen-lezen onderzocht op
dezelfde bronorder, factuur, afgesproken debiteur, betaalmethode, valuta en saldo.
Een bedrag dat bij een andere order past is nooit een matchkandidaat. Meerdere
betalingen die dezelfde factuur claimen vragen onderzoek. PSP-uitbetalingen
blijven in de afzonderlijke bankboek-/tussenrekeningroute.

Als een factuur niet openstaat, worden bestaande verkoopboekingen met dezelfde
YourRef gecontroleerd. Een bestaande boeking wordt niet als ontbrekende factuur
gepresenteerd: controleer dan de eerdere aflettering, credits en conceptstatus.
Een processing-order zonder gevonden factuur krijgt een concrete wachtinstructie.

De lijst vermeldt benodigd bewijs, vervolgstap, verantwoordelijke functie en
hercontrolemoment. Oorspronkelijk en resterend bankbedrag worden met teken
getoond. Een volledig gelezen snapshot wordt in één databasetransactie bewaard.
Bij bronstoringen blijven posten zichtbaar en is ontbrekend orderbewijs geen
matchkandidaat. Dit is een reviewwerklijst; het is nog geen Microsoft-login,
beslisformulier of uitvoerder van financiële mutaties.

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
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=CashflowReceivables
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=CashflowPayments
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=ReadFinancialPayablesList
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialJournals
- Knop en schermnaam bevestigd in de door de operator aangeleverde screenshot.
