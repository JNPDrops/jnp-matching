# Dashboard: uitzonderingen en beslissingen

Vastgelegde gebruikersinstructies van Jasper, bijgewerkt met het besluit van
7 oktober 2026. De zeven afzonderlijke workers zijn live; de centrale werklijst
projecteert hun bestaande dossiers en bewaart menselijke besluiten apart.
Een live worker of deployment bewijst niet dat een dagtaak financieel klaar is.

## Geldend besluit van 6 oktober 2026

Afletteren gebeurt uitsluitend via de native Exact-actie Automatically, met een
vastgelegde selectie per bankboek en teruglezing. Geen handmatige matching,
strict_match/strict_correct, kruisposten of compensatieboekingen tussen orders.
Wat Automatically niet koppelt, blijft open met bronbewijs en vervolgactie.
Deze regel vervangt het eerdere verbod op Automatically en oudere instructies
voor handmatige aflettering. Een scan of aangemaakte regel is geen aflettering.

ICEPAY en Fibonatix verwerken eenmaal per dag de vorige kalenderdag in
Europe/Amsterdam onder een duurzame dagidentiteit. Een eindrapport volgt pas
na alle relevante dagtaken; bij een blokkade heet de dagrun expliciet onvolledig.
De bestaande continue controlefrequenties blijven behouden.

## Betalingsverschillen — besluit 7 oktober 2026, 13:34 CEST

Jasper geeft blijvend akkoord voor een betalingsverschil met absolute waarde
**maximaal EUR 1,00, inclusief EUR 1,00**, zowel onder- als overbetaling, wanneer
het bronordernummer overeenstemt met de eigen Sales Entry. Dit geldt voor alle
verkoopontvangsten, ongeacht PSP/bankboek. Bewijs de eigen order/factuur,
administratie, debiteur, EUR-valuta en actuele resterende bedragen; een gelijk
bedrag zonder orderbewijs is onvoldoende. Behoud de bestaande controles op
dubbele betalingen, refunds/creditnota's, orderstatus en eerdere afletteringen.

Pas de grens toe op het werkelijke resterende verschil van dezelfde order/factuur,
niet per losse regel om grotere verschillen op te knippen. Verschillen boven EUR
1,00 en onzekere of ontbrekende order/factuurkoppelingen blijven ter beoordeling.
Leveranciersbetalingen, belastingen, fees en payouts vallen niet onder deze
Sales Entry-regel.

De uitvoering blijft uitsluitend via Exact Automatically en de bestaande
betalingsverschilrekening. Dit akkoord op zichzelf wijzigt geen globale
Exact-instelling en rechtvaardigt geen handmatige match, kruispost of
compensatieboeking. Bevestig de native verwerking door teruglezing; wat
Automatically niet verwerkt blijft open met een concrete vervolgactie.
Bewaar akkoord, order/factuuridentiteit, bedrag met teken en uitvoering apart.
Een eerdere afgeronde import of native opdracht wordt niet gereset.

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

Voor bewezen eigen verkooporders geldt de EUR 1,00-regel hierboven. Alle andere
verschillen komen in de werklijst totdat een passende beslissing is vastgelegd.
Ontbreekt een bronreferentie, dan geldt de eerder afgesproken boeking op
vraagposten (2000); registreer die zichtbaar, zonder de hele import te blokkeren.
Bewaar ordernummers en transactiereferenties zichtbaar in omschrijving/notitie.

## Dossier bij een goedgekeurd betalingsverschil

Bewaar individuele order-, factuur- en transactiegegevens in het beveiligde
boekingsdossier. Leg daar vast welk verschil is goedgekeurd, door wie en
wanneer, met de beperking tot die concrete uitzondering. De private artefacten
`settlement_before` en `settlement_after` bewaren beslissing en Exact-bewijs.
Een voorbereid script of deployment geldt niet als uitgevoerde boeking.

## Geldende afletterregel — besluit 4 oktober 2026, 21:45 CEST

Deze instructie vervangt uitdrukkelijk het eerdere akkoord om betalingen tussen
orders te verschuiven zolang het verzameldebiteuren-saldo klopt. Iedere betaling
moet bij de eigen order en de bijbehorende Exact-factuur terechtkomen. Anders is
de debiteurenlijst niet bruikbaar om ontbrekende of fout verwerkte posten te vinden.

- Bewijs de koppeling tussen PSP-transactie, Woo-/TD-order en Exact-factuuridentiteit.
- Controleer administratie, debiteur, valuta, bedrag en eventueel deelbetalingen.
- Een gelijk bedrag of dezelfde verzameldebiteur is nooit genoeg voor een match.
- Een reeds afgeletterde factuur vergt controle van de bestaande betaling. Match
  de nieuwe ontvangst niet met een andere order om haar te sluiten.
- Geen open post gevonden betekent niet automatisch dat de factuur ontbreekt:
  zoek ook naar bestaande, reeds afgeletterde facturen en andere debiteuren.
- Een onbekende of ontbrekende factuur laat de ontvangst zichtbaar open bij de
  bronorder. De dashboardwerklijst vermeldt het bewijs en de benodigde vervolgstap.
- Een goedgekeurd betalingsverschil blijft beperkt tot de eigen order en de
  specifiek goedgekeurde omvang. Het is geen toestemming voor een andere betaling.
- Refunds en creditfacturen blijven herleidbaar naar hun oorspronkelijke order.

## Bestaande koppelingen herstellen

Inventariseer afwijkingen tussen bronorder en werkelijke toewijzing over de hele
geïmporteerde periode, inclusief eerder verwerkte weken. Bewaar het oorspronkelijke
dossier. Een afwijkende YourRef is een onderzoekssignaal: bevestig de daadwerkelijk
geselecteerde factuur in Exact voordat een aflettering wordt losgemaakt.

Herstel uitsluitend bewezen verkeerde koppelingen, inclusief de samenhangende
keten van betalingen en facturen. Koppel daarna iedere ontvangst aan de eigen
factuur. Bewaar geïmporteerde betalingen, bronreferenties en totalen. Een eerder
goedgekeurd verschil moet bij de eigen order blijven; voorkom dubbele afboeking.
Bestaande correcte afletteringen blijven intact.

## Controle op voltooiing

### Aanvulling 5 oktober 2026 — indeling en ontbrekende facturen

- Toon vragen per administratie en bankboek, met aantallen per workflowstatus.
  Hetzelfde bankboeknummer in verschillende administraties blijft gescheiden.
  Een ontbrekend bankboek wordt als onbekend getoond, niet uit de PSP geraden.
- Maak ontbrekende inkoopfacturen afzonderlijk filterbaar. Houd deze gescheiden
  van een leveranciersbetaling waarbij nog moet worden vastgesteld welk
  document nodig is.
- Controleer bij ontbrekende verkoopfacturen de eigen webshoporder en status.
  Processing plus een daadwerkelijk niet aangetroffen factuur mag als wachten
  op order/factuurimport worden gepresenteerd. Het bewijst geen toekomstige import.
  Pending/on-hold, annuleringen en completed zonder factuur vragen eigen controle.
  Een bestaande gesloten factuur of meerdere kandidaten is geen ontbrekende factuur.
- Deze indeling geeft geen toestemming voor extra financiële handelingen en
  wijzigt vastgelegde menselijke besluiten niet.

Controleer beide kanten: iedere ontvangst heeft haar eigen factuur (of een
zichtbare, verklaarde uitzondering), en iedere factuur toont het juiste openstaande
saldo. Alleen 'alle betaalde facturen zijn gesloten' of een kloppend totaalsaldo
is onvoldoende. Het dashboard moet bronorder, werkelijke toewijzing en uitvoering
naast elkaar kunnen tonen, met 'verkeerde order gekoppeld' als expliciete reden.

De historische `strict_order_plan`-dossiers blijven als auditbewijs bewaard.
Hun uitvoerinstructies zijn vervangen door het besluit van 6 oktober hierboven;
het bestaan van oude herstelcode is geen toestemming om die uit te voeren.
Dagimporten, native Automatically-uitkomsten en nog open uitzonderingen krijgen
een eigen duurzaam dossier. De werklijst moet de daadwerkelijke bron- en
uitvoeringsstatus tonen en mag geen gesloten factuur als ontbrekend bestempelen.
