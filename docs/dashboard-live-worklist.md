# Live werklijst

De Microsoft-werklijst leest bestaande, private agentdossiers. Er komen geen
Exact- of PSP-aanroepen bij wanneer iemand het dashboard opent of ververst.
Er worden geen nieuwe Render-services aangemaakt.

## Bronnen en reikwijdte

- Bankbetalingen: `jnp_suspense_review`, `jnp_bank_identity_review` en de laatste
  volledige controle in `jnp_allocation_maintenance`. Dit omvat ontbrekende
  facturen, onduidelijke ontvangsten, leveranciersfacturen, refunds, mogelijke
  duplicaten, bedragen, PSP-bankboeken en belastingonderbouwing.
- Fibonatix: `strict_order_plan` uit `fibonatix_import_artifacts`. Eigen bronorder,
  concrete Exact-selectie en teruggelezen uitvoering blijven afzonderlijk.
- Debiteurenroutering: vragen en onzekere wijzigingen uit
  `jnp_debtor_route_queue`, plus posten die op Metorik-orderbewijs wachten.
- ICEPAY: de recentste `icepay_receipt_import_runs` geeft een vraag wanneer het
  resultaat niet bevestigd is. Een geverifieerde import betekent niet dat alle
  ontvangsten zijn afgeletterd; open bankposten verschijnen via de bankreview.

Deze bestaande tabellen hebben geen tenantkolom en horen uitsluitend bij Exact
3977752. Een andere administratie krijgt expliciet 'bron nog niet aangesloten';
zij krijgt nooit de JNP-gegevens. De runtimecontrole vereist ook
`EXACT_DIVISION=3977752`. Nieuwe administraties vereisen aparte bronadapters.

De pagina toont het moment van de bronwaarneming en waarschuwt na twee uur.
Een fout, ontbrekende tabel of nooit uitgevoerde controle is geen lege succesvolle
lijst. Er wordt niet stilzwijgend afgekapt: bronlimieten leveren een zichtbare
onbeschikbare bron op. Bronnen kunnen onafhankelijk mislukken.

Bedragen worden als decimale strings en met hun oorspronkelijke valuta/teken
doorgegeven. Een ontbrekend Exact-restbedrag blijft onbekend. Orderbedragen worden
niet als openstaand saldo gepresenteerd. Er komt geen totaalsaldo over gemengde
valuta, dubbelen of verschillende soorten uitzonderingen.

Overlappende bank-/Fibonatix-dossiers worden alleen op dezelfde concrete bankregel
samengevoegd, met de nieuwste bronwaarneming als leidend. Gelijke bedragen of
orderreferenties bewijzen geen identiteit. Een afwijkende oude YourRef wordt als
onderzoekssignaal getoond; een daadwerkelijk geselecteerde factuur wordt alleen
uit opgeslagen Exact-selectiebewijs getoond.

## Dossierbehandeling en rechten

`viewer` kan de toegewezen administraties en bewijsstukken lezen. `operator` en
`admin` kunnen toewijzen, wachten, heropenen, noteren en een gestructureerde
vervolgstap vastleggen. De bestaande roltoewijzingen worden niet verruimd door
deze release. Automatisch toegelaten medewerkers en Roman blijven dus viewer,
tenzij zij expliciet een andere bestaande dashboardrol krijgen.

Een behandelactie vereist server-side administratierechten, rol, Origin en
CSRF. De server bepaalt de actor uit de Microsoft-sessie. Een oude dossier-versie
of gewijzigd bronbewijs geeft 409. Een nieuw bronresultaat heropent een eerder
besluit ter beoordeling. Een verdwenen bronpost is niet automatisch 'opgelost'.

`dashboard_worklist_cases` bewaart de actuele dossierstatus; de append-only
`dashboard_worklist_events` bewaart ieder besluit/notitie met beslisser, tijdstip,
bronsnapshot en fingerprint. De tabellen worden binnen de bestaande database bij
de eerste behandelactie aangemaakt. De pagina toont de laatste 50 gebeurtenissen;
het volledige gebeurtenissenlog blijft bewaard.

Besluiten hebben uitvoeringsstatus `not_started`. Er is bewust geen financiële
consumer aan deze beslissingen gekoppeld. Alleen expliciet teruggelezen
`matched_verified` uit de bestaande agent kan als uitvoering bevestigd verschijnen.
Er worden vanuit het dashboard geen imports, afletteringen, debtorwijzigingen,
betalingsverschillen of `/cycle`-/`/research`-acties gestart.

## Validatie en inzet

- Bestaande aanmeldingstests plus tests voor administratie/rol/CSRF, onbekende
  restbedragen, tekens/valuta, identiteit-deduplicatie, bronwijzigingen en storingen.
- Python- en JavaScript-syntaxcontrole.
- Bij opstarten draait één interne alleen-lezen broncontrole. De logregel
  `dashboard_worklist_ready` bevat alleen bronstatussen en aantallen, geen
  individuele boekingen, Microsoft-sessies of geheimen.
- Handmatig deployen na actuele main/deploymentcontrole. Automatisch deployen
  blijft uit. Geen aparte infrastructuur of nieuwe API-sleutels nodig.
