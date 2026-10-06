# Zevende worker: leesrapporten en probes

De vier oude losse lifespan-taken worden één toegewezen rol met een duurzame
queue. Catalogus: de bestaande Fibonetics-openpostenvergelijking, de vaste
recente-importcontrole en de Exact-/Paragon-loginprobes. Identiteiten, vaste
perioden, autorisatieverval en oude eenmalige claims blijven behouden.

Een historische claim is geen bewijs van een afgerond rapport. Alleen een
volledig teruggekeerde collectie of een opgeslagen geslaagde probe voltooit de
queue-opdracht. Ontbrekende resultaten, fouten en afgebroken processen blijven
zichtbaar als blocked/uncertain. Herstart van web of worker herhaalt de opdracht
niet. Rapportinhoud blijft in de bestaande private uitvoer; queue-status bevat
alleen operationele metadata. Deze fase voegt geen automatische nieuwe periodes toe.

De reports-owner weigert financiële REST/XML-methoden en browser-save via de
gedeelde grens. Bestaande GET-allowlists en afgeschermde browser-loginprobes
blijven daarnaast gelden. OAuth-tokenrefresh en login zijn toegestaan; de rol
verandert geen boekingen of routeringsinstellingen.

`render/reports-worker.yaml` is een echte native Python-worker met Chromium,
Frankfurt, één 1c-2g-instance, 300s shutdown en handmatige deploys. Begroting:
$25/maand extra compute (Render-prijzen gecontroleerd 5 oktober 2026). Alleen
provisioneren wanneer er uitvoerbaar geautoriseerd rapportwerk is; alle huidige
catalogusopdrachten zijn historisch en verlopen. Geen lege betaalde worker.
Bestaande interne DB en benodigde OAuth/browser-configuratie via env-verwijzingen.
Alleen aanwezige optionele probe-activeringsvelden opnemen, nooit secrets uitlezen.

Eerst echte PostgreSQL-validatie, veilige bootstrap en gate-opencommit. Daarna
`python -m app.agent_control --role reports to-worker`, met status/pause/to-web
als dezelfde duurzame bediening. Nog geen worker aangemaakt of live gezet.
