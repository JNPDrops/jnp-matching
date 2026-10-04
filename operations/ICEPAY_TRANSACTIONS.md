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
- Zet alleen `ICEPAY_TRANSACTION_TASK_ID=icepay-transactions-20261001-03-v8`.
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

`downloaded` betekent dat de bronbestanden zijn opgehaald. Het betekent nog niet
dat de Exact-import gereed is. De refunds moeten inhoudelijk worden gemapt;
uitbetalingen/kosten en bestaande boekingen moeten vóór de uiteindelijke import
worden gecontroleerd. Deze module schrijft niets naar Exact en lettert niets af.

Validatie:

```sh
python -m unittest operations.test_icepay_browser operations.test_icepay_transactions -v
python -m py_compile operations/icepay_transactions.py app/main.py
```
