# ICEPAY ophalen: 1 t/m 3 oktober 2026

De login- en formulierproeven v3–v5 zijn live geslaagd op de bestaande Render-
service. Het portaal gebruikt een readonly daterangepicker met weergave
`dd/mm/yyyy - dd/mm/yyyy`; het bestaande CSV-voorbeeld gebruikt juist Amerikaanse
timestamps `mm/dd/yyyy ... AM/PM`. Deze formaten mogen niet worden verwisseld.

`operations.icepay_transactions` gebruikt uitsluitend de geobserveerde account-
links, kalenderdagen en knoppen. De taak verifieert account 88292 en bedrijfsnaam,
verwijdert de standaard Order time-filter, selecteert Payment time 1–3 oktober
en leest de selectie na toepassen opnieuw terug. De betalings-CSV komt via
Actions → Export payments → Export. De standaardkolommen blijven behouden.
Een download komt direct of via één nieuw CSV-downloadlinkje in Notifications;
meerdere nieuwe kandidaten, verlopen downloads en onbekende schermen stoppen.

De CSV moet exact evenveel rijen bevatten als de gefilterde tabel. Alle betaaldatums
moeten binnen de periode liggen en MerchantID+PaymentID moet uniek zijn. Alleen
merchant 34950 (TD.eu) gaat naar de genormaliseerde selectie. EUR-bedragen worden
met Decimal gelezen; uitsluitend status OK telt als kandidaat-ontvangst.
Ontbrekende Order #-referenties en niet-positieve bedragen worden expliciet geteld.
Een afwijkende referentie tussen bronvelden stopt de validatie.

Refunds worden afzonderlijk op Date created 1–3 oktober gelezen, inclusief alle
Enabled-statussen en alle pagina's. Bronheaders, bronregels, filterbewijs, CSV en
hash blijven privé in de bestaande Postgres-tabel `icepay_transaction_tasks`.
Logregels `ICEPAY_TRANSACTIONS` tonen uitsluitend status, aantallen, totalen, hash
en kolomnamen. Fouten kunnen begrensde formuliermetadata tonen onder
`ICEPAY_TRANSACTION_FORM`; geen cookies, geheimen, klantregels of ruwe fouten.

## Eenmalig activeren

- Gebruik de bestaande ICEPAY_WEB_USERNAME/PASSWORD en optionele TOTP-configuratie.
- Zet alleen `ICEPAY_TRANSACTION_TASK_ID=icepay-transactions-20261001-03-v15`.
- Render env-merge (`replace=false`) start zelf één deployment. Auto-deploy blijft uit.
- De taak vervalt op 5 oktober 18:00 UTC en claimt vóór browserstart één poging.
- Bestaande claims blijven intact; een herstart herhaalt geen export.
- Geen nieuwe infrastructuur, publieke route of Exact-schrijfopdracht.

V1 stopte op 4 oktober 19:38 UTC met `date_control_missing` vóór de export.
De aansluitende formulierdiagnose toont de verwachte datumvelden en ongewijzigde
standaardperiode. V2 wacht expliciet op zichtbaarheid van het filterveld en de
kalender, op het wissen van de orderdatum en op sluiting van het ingediende
exportvenster. De v1-claim blijft intact; er is geen export/import herhaald.

V2 selecteerde en verifieerde de periode maar stopte vóór de export bij de
tabelteller. V3 telt daarom de daadwerkelijk geobserveerde PaymentID-checkboxes
op alle pagina's (maximaal 5000). De CSV moet niet alleen hetzelfde aantal regels
bevatten, maar exact dezelfde PaymentID-verzameling. Er wordt geen betekenis
meer afgeleid uit de tekstvorm van de betalingspaginavoettekst.

V3 stopte vóór de export bij een onverwachte browserfout tijdens paginering.
V4 leest PaymentID-checkboxes en de toestand van Next atomair uit de zichtbare
DOM en wacht op een stabiele momentopname na wisselen van paginagrootte.
Onverwachte fouten geven uitsluitend de foutklasse en publieke bronfunctie/-regel
terug; nooit de fouttekst. Alle eerdere claims blijven intact.

V4 kwam door de volledige selectie en PaymentID-controle en bereikte de exportfase.
De notificatieknop heette daarna `Notifications, 1 unread notification`; de exacte
oude naam werd niet meer gevonden. V5 ondersteunt deze waargenomen naam en maakt
**geen nieuwe export**. De parent leest uitsluitend het vastgelegde periode-/ID-
bewijs van v4 uit de eigen taaktabel en geeft dat zonder databasegeheim door aan
het child. De actuele ID-selectie moet daarmee overeenkomen. Daarna worden
maximaal vijf bestaande CSV-downloads bekeken; alleen een CSV met exact die IDs
wordt bewaard en verder gevalideerd. Oude claims blijven intact.

V5 vond 38 PaymentID's, maar geen download met exact dezelfde volledige inhoud.
V6 bewaart de gedownloade kandidaten privé met hash en rapporteert alleen
rijaantallen/ID-overlap. Een ruimere export mag uitsluitend worden beperkt tot
de 38 onafhankelijk in de UI gecontroleerde IDs als elk daarvan precies één keer
in de bron staat. Zowel het origineel als de afgeleide selectie worden bewaard;
de afgeleide selectie moet alle bestaande datum-/bedrag-/merchantcontroles door.
UTF-16 met BOM en een expliciete Excel `sep=`-regel worden ondersteund. Er wordt
nog steeds geen nieuwe export gestart en niets naar Exact geschreven.

V6 bewaarde één leesbare bestaande CSV met 114 regels, nul overlappende IDs en alle
38 gevraagde IDs ontbrekend. De foutdiagnose toont het nog geopende Notifications-
venster. Een Escape sloot dit kennelijk niet; daardoor konden achterliggende
knoppen niet via hun toegankelijke rol worden bediend. V7 sluit het venster
expliciet via de geobserveerde Close-knop en wacht tot het weg is. Daarna wordt
één gerichte export gestart. De taak bewaart `not_started`, `submit_attempted` of
`submitted` als afzonderlijk exportbewijs; een herstart herhaalt niets. De CSV
moet nog steeds de volledige onafhankelijke ID-selectie dekken. Exact blijft
ongewijzigd.

V7 heeft de gerichte export aantoonbaar ingediend (`export_state=submitted`).
ICEPAY toont een extra notificatie, maar er verscheen binnen de wachttijd geen
nieuwe CSV-URL. V8 dient niets opnieuw in: hij controleert de bestaande downloads
op de vastgelegde IDs en leest uitsluitend de exportmeldingen, zonder links,
e-mailadressen of credentialgerelateerde teksten te loggen.

V8 las de concrete ICEPAY-melding: beide moderne exportpogingen leverden
`0 rows exported. 38 rows failed to export.` De enige beschikbare CSV is nog de
eerdere export met 114 andere IDs. V9 gebruikt eenmaal de al geobserveerde
`Export payments (legacy)`-optie voor exact dezelfde gecontroleerde selectie.
Het moderne exportpad wordt niet herhaald. Als legacy eerst een configuratievenster
opent, stopt de taak met de zichtbare formuliermetadata; er wordt niet blind op
een onbekende verzendknop gedrukt. Alle bestaande broncontroles blijven gelden.

V9 opende de legacy-bevestiging met alleen Close, Export (submit) en Cancel;
er waren geen extra invoervelden. V10 bevestigt deze geobserveerde Export-knop
eenmaal en wacht op de download. De 38 onafhankelijke IDs blijven verplicht.

V10 diende legacy in, maar die leverde niet direct een browserdownload. V11
haalt uitsluitend bestaande exports uit Notifications op, inclusief de later
beschikbare legacy-export. Er wordt niets opnieuw ingediend. Refunds worden ook
gelezen als nog geen passende CSV bestaat. Begrensde zichtbare tabelregels blijven
privé als aanvullend bronbewijs; logs tonen uitsluitend kolomnamen en aantallen.

V11 heeft de legacy-CSV met exact 38 IDs opgehaald (SHA-256
`d623e07ceba28cbe211053933f1bd91fb71d985a6c382441e5ec155d986973ef`) en nul
refunds vastgesteld. V12 valideert uitsluitend deze opgeslagen bron opnieuw,
zonder browser of nieuwe export. Checkout-kolommen zijn optioneel als een
Description-kolom aanwezig is; alleen een expliciet Order #-nummer uit een
beschikbaar referentie-/beschrijvingsveld wordt gebruikt. Alle aanwezige velden
worden op onderlinge conflicten gecontroleerd. Financiële velden blijven verplicht.
De schema-diagnose bevat alleen kolomnamen en aantallen ongeldige ID-/rijvormen.

V12 bevestigde het 45-koloms legacy-formaat zonder Checkout-kolommen, maar de
moderne Amerikaanse datumparser wees de bron af als buiten de periode. V13
accepteert een legacy-datumconventie alleen als de complete ID-set exact gelijk
is aan het onafhankelijke UI-bewijs en alle datums onder precies één interpretatie
binnen 1–3 oktober vallen. Bedragen en Order #-conflicten blijven strikt.
Na succesvolle validatie volgt een uitsluitend lezende Exact-controle van dagboek
27, debiteur 109419, grootboeken 1100/1317/1360 en bestaande 2026-boekingen op
het ICEPAY-grootboek/debiteur. Er is nog steeds geen upload- of aflettercode.

V13 kon nog geen volledige datuminterpretatie vaststellen. V14 toont daarom de
begrensde betaal-tijdwaarden en statuswaarden uit uitsluitend deze vaste bron;
geen klant- of credentialvelden. De Exact-configuratie wordt onafhankelijk gelezen,
met `source_validated=false` en `ready=false`, totdat de broncontrole slaagt.

V14 toonde Amerikaanse timestamps, waaronder 30 september 22:33:54, en status
OK/ERR. Dit wijst op UTC-export tegenover de Nederlandse portaalperiode. V15
controleert de UTC→Europe/Amsterdam-omzetting tegen OrderTime van **alle 38**
onafhankelijk opgeslagen UI-rijen voordat hij betaaldatums omzet. Een ontbrekende
of afwijkende vergelijking blokkeert; er wordt geen uurcorrectie gekozen om een
regel passend te maken. Een geïsoleerde leesbot haalt daarnaast Statements en
Transfers op; alleen rekeningoverzichten, geen nieuwe export of financiële actie.
De leescontrole in Exact bevestigde bankboek 27 en grootboek 1317 leeg, debiteur
109419 aanwezig (Verzameldebiteur Icepay). Na bronvalidatie worden IDs/referenties
tegen de opgeslagen Exact-controle vergeleken.

`downloaded` betekent dat de bronbestanden zijn opgehaald. Het betekent nog niet
dat de Exact-import gereed is. De refunds moeten inhoudelijk worden gemapt;
uitbetalingen/kosten en bestaande boekingen moeten vóór de uiteindelijke import
worden gecontroleerd. Deze module schrijft niets naar Exact en lettert niets af.

Validatie:

```sh
python -m unittest operations.test_icepay_browser operations.test_icepay_transactions -v
python -m py_compile operations/icepay_transactions.py app/main.py
```
