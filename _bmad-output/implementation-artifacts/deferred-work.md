
## Deferred from: code review of 2-1-creer-agent-template-depuis-archetype.md (2026-09-05)

- R2 — Route `/config/agents` : AC5 et T5.4 sont contradictoires. Le bouton « Nouveau » reste sur `/config/` pour la 2.1 ; créer la vraie liste et sa route dans la Story 2.2.
- R3 — Page post-création : AC5 et T5.3 sont contradictoires. Le placeholder ID-only reste en 2.1 ; charger et afficher nom, archétype et version dans la Story 2.2.
- R11 — ORM/migration `uq_agent_template` : divergence NULLS NOT DISTINCT avérée, impact medium non vérifié. Examiner le DDL et Alembic autogenerate avec les versions verrouillées ; fixtures actuelles utilisent les migrations. Source : blind-hunter ; backend/alembic/versions/20260508_000000_agent_templates_unique_nulls_not_distinct.py:61 et backend/src/agentive_backend/infra/db/models.py:217.
- R12 — NOTIFY post-commit attendu sans borne locale : impact medium non vérifié, mécanisme publisher hérité. Injecter un blocage connexion/query et examiner les timeouts effectifs avant de définir un délai dans le service. Source : edge-case-hunter ; backend/src/agentive_backend/features/m2_agent_registry/service.py:159.
