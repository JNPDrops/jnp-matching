# Dashboard: bankboeken en factuurvragen

Gebouwd op main `cf75100f845d1296eaaa60f85225caf3c656e4f2`, 5 oktober 2026.
Deze wijziging is onafhankelijk van de onvoltooide workeropsplitsing in PR #82.

- Nieuw bankboekoverzicht met aantallen open/wachtende/besloten vragen en
  ontbrekende inkoopfacturen; doorklikken selecteert dat bankboek in de werklijst.
- Bankboekfilter combineert met administratie, betaalstroom, factuurcategorie en
  bestaande vraagsoort. Onbekende bankboeken blijven apart. Geen saldo-optellingen.
- Groepen: ontbrekende inkoopfacturen, overige inkoopdocumentcontrole,
  webshoporder wacht op import, verkoopfactuur/ordercontrole en overige vragen.
- Bestaande bankdossiers bevatten al Metorik-orderstatus en Exact-factuurcontrole.
  De UI gebruikt alleen dit vastgelegde bewijs. Processing is een aanwijzing
  voor verdere importcontrole, niet een gegarandeerde toekomstige import.
- Fibonatix `refresh_missing` doet naast de bestaande Exact-reads nu een gebundelde
  Metorik-read voor maximaal vijf eigen orders. Eerder gecontroleerde ontbrekende
  facturen kunnen opnieuw worden gelezen, oudste controle eerst. Opgeslagen
  financiële pogingen en historie blijven behouden. Geen writes naar Exact.
- De dashboardpagina zelf doet geen extra PSP/Exact-calls. Behandelbesluiten,
  Microsoft-toegang en financiële uitvoering blijven ongewijzigd.

Validatie: 48 unittest-tests (inclusief browser-JavaScriptfiltertest via Node) en
32 bestaande pytest-tests voor bankafhandeling/strikte ordermatching geslaagd.
JavaScript-syntax en `git diff --check` geslaagd. Uitsluitend synthetische gegevens.

Live gegevenscontrole: de laatst gelezen bankagentscan van 5 oktober 21:19 CEST
bevat 21 posten met `supplier_invoice_missing`; in de banklijst waren geen
vastgelegde processing-orders. Dit is geen complete actuele uitspraak over de
afzonderlijke Fibonatix-vragen. Rechtstreekse PostgreSQL-lezing via de plugin is
geblokkeerd door de interne netwerktoegang; er is niets openbaar gemaakt.

Dashboard: https://jnp-matching.onrender.com/dashboard/ (Microsoft-login).
Nog niet gedeployed bij aanlevering. Productie blijft handmatig deployen.
Geen deployment midden in een financiële taak zonder bewezen drain; nachtelijke
verwerking moet actuele main en dit dossier lezen en de ontbrekende
Fibonatix-webshopcontroles via de bestaande geautoriseerde integratie uitvoeren.
