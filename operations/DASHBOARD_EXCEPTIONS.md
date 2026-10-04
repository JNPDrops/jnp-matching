# Dashboard: uitzonderingen en beslissingen

Vastgelegde gebruikersinstructies van Jasper, 4 oktober 2026 (CEST).
Dit document is een blijvende implementatie-eis. Het bestaande dashboardconcept
is een demonstratie; deze werklijst en de uitvoering zijn daarmee nog niet live.

## Werklijst

Vragen over betalingsverschillen, ontbrekende facturen, onduidelijke betalingen
en overige afletteruitzonderingen moeten in de centrale dashboardwerklijst
komen, zoals besproken in de dashboardchat. Herhaalde vragen in losse chats
zijn niet de beoogde normale afhandeling.

Aansluiten op het bestaande concept: zoeken/filteren per administratie, status
`open`, `waiting` (Wachten op informatie) en `decided`, Aan mij toewijzen en
Beslissing vastleggen. Bewaar keuze, toelichting, beslisser en tijdstip in het
dossier. Toon de uitvoering apart van de beslissing: een akkoord betekent nog
niet dat Exact de boeking heeft verwerkt. Toon voltooiing pas na teruglezing,
en bewaar een fout of onzekere uitkomst zonder automatische tweede boeking.

Elke uitzondering toont minimaal:

- Administratie, betaalprovider, debiteur en dagboek.
- Oorspronkelijke TD-order, Woo-ordernummer en betaaltransactie-ID.
- Werkelijk gekoppelde Exact-factuur/order, als die afwijkt van de bronorder.
- Factuurbedrag, betaling, werkelijk resterend bedrag met teken en verschil.
- Reden van de uitzondering, bewijs uit bron en Exact en voorgestelde actie.
- Acties zoals betalingsverschil boeken, wachten op informatie of toewijzing
  corrigeren, voor zover passend bij de concrete uitzondering.
- Vastgelegde beslissing en afzonderlijk uitvoeringsresultaat met Exact-bewijs.

Geen algemene automatische afboekgrens is afgesproken. Toekomstige verschillen
komen in de werklijst totdat een expliciete passende beslisregel is vastgesteld.
Ontbreekt een bronreferentie, dan geldt de eerder afgesproken boeking op
vraagposten (2000); registreer die zichtbaar, zonder de hele import te blokkeren.
Bewaar ordernummers en transactiereferenties zichtbaar in omschrijving/notitie.

## Dossier bij een goedgekeurd betalingsverschil

Bewaar individuele order-, factuur- en transactiegegevens in het beveiligde
boekingsdossier. Leg daar vast welk verschil is goedgekeurd, door wie en
wanneer, met de beperking tot die concrete uitzondering. De private artefacten
`settlement_before` en `settlement_after` bewaren beslissing en Exact-bewijs.
Een voorbereid script of deployment geldt niet als uitgevoerde boeking.

De gebruiker accepteert aflettering tussen orders binnen een verzameldebiteur
als het saldo klopt en betaalde orders niet meer openstaan. Een afwijkende
YourRef is op zichzelf geen reden voor een massale herstelactie. Toon wel de
bronorder en werkelijke Exact-toewijzing apart, zodat het dossier navolgbaar is.

## Verbetering voor toekomstige aflettering

Streef naar koppeling op bewezen factuuridentiteit plus bedrag vóór aflettering.
Alleen een gelijk bedrag is onvoldoende om dezelfde order te bewijzen. Toon
bronreferentie en werkelijke toewijzing apart en houd onduidelijke gevallen in
de werklijst. Bestaande geaccepteerde toewijzingen hoeven hiervoor niet opnieuw.
Volledig terugbetaalde orders zijn aparte refund-/creditfactuurgevallen en mogen
niet als nog onbetaalde normale orders worden gepresenteerd.
