# JNP Matching v0.9

Veilige Exact Online matcher voor WooCommerce-orders op verzameldebiteur 100100.

## Belangrijkste wijziging
De bankomschrijving hoeft alleen het numerieke ordernummer te bevatten, bijvoorbeeld `VERCAIGNE BO 48451`. Exact gebruikt voor de salesentry `YourRef = TD48451`.

Daarom maakt v0.9 **geen generieke `Words=TD`-regel**. In plaats daarvan kan de app per open webshoporder een Exact AllocationRule voorbereiden:

- `Account = <GUID van 100100>`
- `Words = 48451`

Nieuwe geïmporteerde bankregels die `48451` bevatten kunnen hierdoor door Exact aan verzameldebiteur 100100 worden toegewezen. Daarna kan JNP Matching direct afletteren via MatchSets.

## Bestaande 1360-backlog
De publieke REST API ondersteunt geen normale update van een reeds bestaande BankEntryLine. Voor bestaande geïmporteerde regels kan Exact na het aanmaken van de orderregels in de UI opnieuw de automatische toewijzing uitvoeren. Daarna verschijnen ze onder `/allocated` en kunnen ze direct worden gematcht.

## Safety flags
- `ENABLE_ORDER_RULE_WRITES=false`
- `ENABLE_DIRECT_MATCH_WRITES=false`

Laat beide uit totdat één orderregel en één directe match gecontroleerd zijn.
