# Belastingherkenning — James n Parson, 4 oktober 2026

Operatoropdracht: RSIN **867393051**, Exact-administratie **3977752**.
Herken betalingen van en aan de Belastingdienst en leid het laatste jaarcijfer
af naar het dichtstbijzijnde jaar. Deze uitbreiding verandert de bestaande
debiteurenroutering en Woo/bosci-regels niet.

## Werkende herkenning

- Controlecijfer van een 16-cijferig betalingskenmerk en de RSIN-elfproef.
- Vpb (`V`), btw (`B`), loonheffingen (`L`) en naheffingen (`F`/`A`).
- Ook een volledig herkenbaar aanslagnummer in de omschrijving wordt gelezen.
- Jaar: dichtst bij het **bankjaar**, zodat historische transacties bij een
  latere scan hetzelfde resultaat behouden. Bij exact vijf jaar verschil het
  eerdere jaar. Een expliciet tweecijferig jaar mag deze uitkomst niet tegenspreken.
- De oorspronkelijke bankdatum, het ondertekende bedrag, de betaalrichting,
  subnummers en periodecodes blijven behouden. Geen verschuiving van het boekjaar.
- Meerdere aanslagen, afwijkende RSINs, ongeldige/ontbrekende controlecijfers,
  willekeurige kenmerken en niet ondersteunde soorten blijven ter beoordeling.
- Rente, boetes, verrekeningen en kosten worden niet uit een nummer verzonnen.
  Naheffingen, definitieve/navorderings-Vpb en teruggaven vragen om de specificatie.

Bevestigde casus: `6739305619301120` wordt bij bankjaar 2026 herkend als
`8673.93.051.V.61.0112`, voorlopige Vpb 2026, periodecode `0112`.

## Ingebouwd in de bestaande agent

De bestaande kandidatenlijst herkent belastingregels **vóór** de selectie van
positieve webshopontvangsten. Belastingkenmerken worden niet meer als een
webshopordernummer opgezocht. `TAX_IDENTIFIED` betekent herkenning, geen boeking.

Een GET-only achtergrondtaak leest iedere 30 minuten gewijzigde belastingregels;
de eerste scan leest de historische selectie zonder datumgrens of `$top`-cap.
Alle `__next`-pagina's worden gevolgd. Een onvolledige scan verplaatst de cursor
niet. Een overlap van vijf minuten vangt gelijktijdige wijzigingen op. Rekening-
metadata wordt maximaal eenmaal per dag opnieuw gelezen. API-budgetreserve 150;
geen app/sleutelwissels of nieuwe infrastructuur. Bestaande pauzes blijven staan.

Opslag in bestaande database: `jnp_tax_control` en `jnp_tax_observations`.
`/api/tax/status` en `/health` tonen alleen operationele status en aantallen.
`/api/tax/report` toont de waarnemingen via de bestaande Allocation-operatorsessie.
De rapportage vermeldt expliciet of de maximaal 500 getoonde regels onvolledig zijn.
Logs bevatten aantallen, geaggregeerde belastingtemplates en kandidaat-grootboekgegevens,
geen bankbedragen, betalingskenmerken, bankomschrijvingen of OAuth-gegevens.

## Rekeningvoorstel en grens aan automatisch boeken

`tax_policy.json` bevat de concrete rekeningmapping. Alleen een uniek bestaande,
niet-geblokkeerde balansrekening mag als geverifieerd rekeningvoorstel verschijnen.
Zonder geverifieerd nummer toont de agent bestaande kandidaten; hij kiest geen
rekening op alleen een gedeeltelijke naam. Een betaling of teruggaaf is geen
nieuwe btw-belaste kostenpost. De belastingsoort bepaalt niet de eventuele
uitsplitsing naar rente/boete of de bijbehorende reeds geboekte openstaande post.

## Exact-toewijzingsregels (4 oktober 2026)

De gebruiker heeft het toevoegen van volledige gegenereerde betalingskenmerken
als toewijzingsregel toegestaan. De API-route is `POST cashflow/AllocationRule`
(beta). De agent maakt uitsluitend regels met `Account`, `GLAccount` en het
volledige 16-cijferige `Words`-kenmerk; geen nieuwe btw-code of financieel bedrag.
De bestaande GET-only banktransportlaag blijft GET-only.

Live gecontroleerde balansrekeningen:

| Soort | Rekening | Onderbouwing |
| --- | --- | --- |
| Vpb | 1500 Vennootschapsbelasting | Enige actieve Vpb-balansrekening; bevestigd Vpb-kenmerk stond nog op 1400 |
| Btw | 1770 Afdrachten omzetbelasting | Bestaande B01-betaling voor kwartaal 3 van 2025 stond al op 1770 |
| Loonheffingen | 1600 Af te dragen loonheffing | Actieve loonheffingenbalansrekening; nog geen geldig L-subnummer in de ingelezen historie |

De generator maakt voor vorig, huidig en volgend kalenderjaar:
- Vpb voor het bevestigde kalenderjaartijdvak 0112, voorlopige aanslagen 0 t/m 5;
- B01-kwartaalaangiften met tijdvakken 21/24/27/30;
- L-kenmerken pas na een geldig, regulier uitgaand kenmerk in de betaalhistorie
  waaruit subnummer en maand/vierweken/halfjaar/jaarfrequentie blijken.

Bij start in 2026 zijn dit 30 regels (18 Vpb en 12 btw). Alle kenmerken krijgen
het officiële controlecijfer en worden weer gedecodeerd. Een gegenereerd kenmerk
betekent **niet** dat de aanslag daadwerkelijk is opgelegd of betaald. Er worden
geen naheffingen, definitieve Vpb of navorderingen vooruit gegenereerd.

`jnp_tax_rules` bewaart per kenmerk het doel, de aanmaakintentie en het teruggelezen
Exact-ID. Bestaande gelijke regels worden hergebruikt; afwijkende of dubbele regels
worden niet overschreven. Ook brede bestaande belastingregels met een grootboek
leiden tot een conflict. De bestaande Belastingdienst-IBAN-regel zonder grootboek
blijft staan. Onzekere POSTs worden uitsluitend teruggelezen, nooit blind herhaald.
Maximaal 40 nieuwe regels per scan, met API-budgetreserve en dezelfde operatorpauze.

**De regels zijn geen bevestiging dat bankregels zijn geboekt.** Gebruik in Exact
`Automatically` / `Automatisch` om de regels op eerder geïmporteerde afschriften
te laten toepassen. De agent drukt deze knop niet in. De API voor `BankEntryLines`
ondersteunt GET/POST, geen PUT; bestaande bankboekingen worden niet vervangen.

Exact-toewijzingsregels hebben geen filter voor betaalrichting, bedrag, rente of
boetes. Een teruggaaf met hetzelfde kenmerk kan dus dezelfde balansrekening krijgen.
De reviewstatus in de agent kan toepassing van een Exact-regel **niet blokkeren**.
Controleer rente, boetes, gemengde betalingen, verrekeningen en reeds geboekte
openstaande aanslagen afzonderlijk. Automatische rekeningtoewijzing is geen garantie
op een volledig correcte uitsplitsing of aflettering.

## Validatie en bronnen

`python -m pytest operations/test_tax_reference.py operations/test_tax_allocation.py -q`

Officiële bronnen gecontroleerd 4 oktober 2026:
- https://odb.belastingdienst.nl/wp-content/uploads/2025/07/Specificatie-Betalingskenmerk_bepaling_1.5.pdf
- https://www.acceptgiro.nl/wp-uploads/Cu_RR_Ac_Bijlage_G.A_Formspecs.pdf (gewogen modulus 11, paragraaf 3.5.3)
- https://www.betaalvereniging.nl/kennisbank/rekeningen-en-diensten/betalingskenmerk/
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialTransactionBankEntryLines
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialTransactionBankEntries
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=CashflowAllocationRule
- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=FinancialGLAccounts

- https://start.exactonline.nl/docs/HlpRestAPIResourcesDetails.aspx?name=CRMAccounts
- https://download.belastingdienst.nl/belastingdienst/docs/tijdvakcodes-aangiftedatums-betaaldatums-lh2101t62fd.pdf
