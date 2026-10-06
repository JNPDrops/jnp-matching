# HTTP-startpunten bij de opsplitsing

Inventarisatie 5 oktober 2026, branch refactor/agent-separation. Er zijn bij deze
controle geen endpoints aangeroepen. Alleen broncode is onderzocht.

| Route | Werking in deze branch | Overdracht |
| --- | --- | --- |
| Fibonatix `POST /ops/fibonatix-20261002/...` (`/{action}`, `/strict/{mode}`, `/audit_order_matches`) | Bestaande autorisatie; idempotente databaseopdracht; uitvoering alleen door toegewezen Fibonatix-owner | Gebouwd; historische expiratie blijft gelden |
| Fibonatix `/prepare` | Bewaart/valideert het vaste bronbestand; geen financiële uitvoering | Bestaande dossier-ID en claims behouden |
| Woo IBAN/reference ontvangst | HMAC, validatie, duurzame bestaande Woo-queue | Gebouwd; geen directe Exact-write vanuit HTTP |
| Woo check/status | Leest status; geen financiële uitvoering | Blijft bij web |
| `POST /allocation/probe` | Alleen GET-proef via Allocation met gedeeld quota | Blijft bij web; tokenrefresh gedeeld vergrendeld |
| `POST /order-rules/{order_number}/create` | Legacy directe AllocationRule-POST, achter bestaande featureflag | Nog geen duurzame opdrachtadapter; moet bewezen uitgeschakeld en gedraind zijn vóór productieoverdracht |
| `POST /direct-match/{bank_line_id}/execute` | Legacy directe XML MatchSets-call, achter bestaande featureflag | Nog geen duurzame opdrachtadapter; moet bewezen uitgeschakeld en gedraind zijn vóór productieoverdracht |
| Dashboard werklijstwijziging | Microsoft-rol, administratiecontrole, CSRF, bewaart menselijke beslissing | Geen Exact-schrijfopdracht; beslissing is niet uitvoering |
| OAuth callbacks | Alleen de bestaande verbinding en tokenopslag | Gedeelde database-locks behouden |
| Maintenance/tax/status/artifacts | Lezen, geen financiële start | Web houdt inzage in duurzame status |
| `/cycle`, `/research` | Geen route in de huidige app.main/router-inventarisatie | Niet gebruikt; blijft verboden in deze opdracht |

De twee legacy financiële HTTP-knoppen zijn niet stilzwijgend omgezet naar nieuwe
financiële opdrachten of als veilig voor parallelle uitvoering aangemerkt. Ze
zijn een expliciete bootstrapvoorwaarde. Controleer de booleans
`ENABLE_ORDER_RULE_WRITES` (inclusief fallback `ENABLE_ALLOCATION_RULE_WRITES`) en
`ENABLE_DIRECT_MATCH_WRITES`, stop nieuwe verzoeken en bewijs dat lopende verzoeken
klaar zijn. Een vlag op false maakt een reeds lopend verzoek niet ongedaan.

De runtime-gates blijven gesloten; alleen geslaagde unit-tests openen ze niet.
Daarom zijn huidige productie en bestaande featureflags in deze bouwfase niet
gewijzigd. Voor later opnieuw activeren van deze twee knoppen is eerst een
opdrachtadapter met eigen volledige audit/readback nodig.
