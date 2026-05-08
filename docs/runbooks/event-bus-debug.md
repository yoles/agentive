# Runbook — debug du bus d'événements

Sprint 0 / Story 1.4 — `shared.event_bus`

> ⚠️ **Note RLS** : les snippets `psql` ci-dessous utilisent le rôle
> `agentive_owner`, qui est **propriétaire de la table** et donc exempt de
> Row Level Security. C'est pratique en dev (vous voyez toutes les rows,
> tous tenants confondus) mais **trompeur en environnement multi-tenant**
> hardened — un opérateur qui copie-colle un snippet pourrait croire que la
> table est vide alors qu'elle ne l'est juste pas pour son tenant.
> Pour des queries tenant-scopées, ouvrez la session avec le rôle
> `agentive_app` ET exécutez d'abord
> `SET app.tenant_id = '<uuid>'` (ou `SET LOCAL` dans une transaction).

---

## Inspecter le backlog (events non-traités)

```sh
docker compose exec db psql -U agentive_owner -d agentive -c "
SELECT event_type, COUNT(*) AS pending
FROM outbox_events
WHERE processed_at IS NULL
GROUP BY event_type
ORDER BY pending DESC;
"
```

Backlog attendu en régime normal : **0**. Si > 100, vérifier que le worker
tourne (`grep event_bus.outbox_worker_started` dans les logs container
backend).

## Forcer un replay manuel

Re-mettre tous les events d'un type à `NULL` pour forcer le replay au prochain
poll (5s par défaut) :

```sh
docker compose exec db psql -U agentive_owner -d agentive -c "
UPDATE outbox_events
SET processed_at = NULL
WHERE event_type = 'system.health.checked'
  AND created_at > NOW() - INTERVAL '1 hour';
"
```

⚠️ Idempotent côté handler par construction (Sprint 0 — pas de state
mutant). Sprint 1+ : valider que les handlers concernés sont rejouables avant
d'utiliser cette commande.

## Debugger un handler qui crash en boucle

```sh
docker compose logs backend --since 5m | grep event_bus.handler_failed
```

Le log structuré contient `event_id`, `event_type`, `handler_module`,
`error_type`. Pour l'event coupable :

```sh
docker compose exec db psql -U agentive_owner -d agentive -c "
SELECT id, event_type, payload, created_at
FROM outbox_events
WHERE id = '<event_id_from_log>';
"
```

Si le handler est définitivement cassé : marquer manuellement l'event comme
processed pour le sortir du replay loop (last resort — mieux : fix the
handler) :

```sh
docker compose exec db psql -U agentive_owner -d agentive -c "
UPDATE outbox_events SET processed_at = NOW()
WHERE id = '<event_id>';
"
```

## Vérifier que LISTEN est actif côté Postgres

```sh
docker compose exec db psql -U agentive_owner -d agentive -c "
SELECT pid, application_name, state, query
FROM pg_stat_activity
WHERE query ILIKE '%LISTEN%' OR query ILIKE '%notify%';
"
```

On attend **1 ligne** (la connexion dédiée du `OutboxWorker`) avec
`state = 'idle'` et `query` contenant `LISTEN agentive_outbox`.

Si **0 ligne** : le worker n'est pas démarré ou a crashé. Vérifier
`agentive_startup` dans les logs et l'erreur éventuelle après.

Si **> 1 ligne** : multiple instances backend tournent — c'est un signal
de migration vers Redis Streams (cf
[`event-bus-migration-trigger.md`](../decisions/event-bus-migration-trigger.md)).

## Mesurer la latence publish → handler en local

```sh
docker compose exec backend uv run python -c "
import asyncio, time
from uuid import uuid4
from agentive_backend.shared.event_bus import publish_and_commit, subscribe
from agentive_backend.infra.db.session import get_session_factory

async def main():
    received_at = []
    async def handler(event):
        received_at.append(time.monotonic())
    await subscribe('system.health.checked', handler)

    factory = get_session_factory()
    async with factory() as s:
        sent_at = time.monotonic()
        await publish_and_commit(s, 'system.health.checked',
            {'status':'ready','checks':{'db':'ok'}}, correlation_id=uuid4())

    await asyncio.sleep(2)
    if received_at:
        print(f'latency_ms = {(received_at[0]-sent_at)*1000:.1f}')
    else:
        print('NO HANDLER INVOCATION — worker likely down')

asyncio.run(main())
"
```

Latence cible : **< 200 ms** end-to-end en local Docker.

## Métriques Prometheus exposées

| Métrique | Type | Labels | Usage |
|---|---|---|---|
| `agentive_event_bus_publish_seconds` | Histogram | `event_type` | Latence `publish()` |
| `agentive_event_bus_outbox_backlog` | Gauge | — | Rows non-traités (sample 30s) |
| `agentive_event_bus_handler_seconds` | Histogram | `event_type`, `handler_module` | Durée par handler |
| `agentive_event_bus_handler_failures_total` | Counter | `event_type`, `handler_module`, `error_type` | Échecs handler |

L'endpoint `/metrics` est ajouté en Story 1.9 (observability foundations).
Jusque-là les métriques sont accessibles in-process via
`prometheus_client.REGISTRY.collect()`.
