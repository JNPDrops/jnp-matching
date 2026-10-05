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

Het beleid verbiedt de oude Automatically-actie en afwikkeling met een ontvangst
van een andere order. De technische blokkade in `source_order_policy.py` is via `app/main.py` aan de
applicatie gekoppeld. Na een handmatige deployment bevestigt de beveiligde
`order-policy`-route of de draaiende versie deze blokkade daadwerkelijk gebruikt.
De gerichte herstelfunctie in `strict_order_matching.py` is geïmplementeerd.
Het private `strict_order_plan` bewaart voortgang, daadwerkelijke factuurselectie,
uitzonderingen en bewijs na iedere opslag. Een deployment betekent niet dat de
historische koppelingen al volledig zijn hersteld. Zie STRICT_ORDER_MATCHING.md.
