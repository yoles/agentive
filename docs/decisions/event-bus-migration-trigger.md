# Event bus — déclencheur de migration Postgres LISTEN/NOTIFY → Redis Streams

**Statut** : Accepté (déclencheur, pas la migration elle-même)
**Date** : 2026-04-30
**Story d'origine** : 1.4 (Core event bus)
**Story d'exécution future** : Sprint 4+ si SaaS multi-instance

---

## Contexte

L'implémentation MVP utilise PostgreSQL `LISTEN/NOTIFY` + Outbox Pattern
(`shared.event_bus`). Architecture (ligne 794-800) confirme un headroom de
~50× sur la charge MVP (3-5k events/s capacité vs < 100 events/s estimé).

Cette ADR fixe les **conditions objectives** qui obligent à exécuter la
migration vers Redis Streams pour éviter de la déclencher trop tôt (coût
opérationnel, dépendance supplémentaire) ou trop tard (latence dégradée
visible utilisateur).

## Déclencheurs (OR — l'un suffit)

1. **Latence p95 du chemin de publication end-to-end soutenue** :
   - **somme** `agentive_event_bus_publish_seconds` + `agentive_event_bus_notify_seconds`
     p95 > **100 ms** sustained sur **24 h**.
   - Requête Prometheus :
     ```promql
     histogram_quantile(0.95, sum(rate(agentive_event_bus_publish_seconds_bucket[5m])) by (le))
     +
     histogram_quantile(0.95, sum(rate(agentive_event_bus_notify_seconds_bucket[5m])) by (le))
     ```
   - **Pourquoi deux métriques ?** Les deux phases ont des modes de panne
     distincts : `publish_seconds` mesure l'INSERT (contention DB / RLS),
     `notify_seconds` mesure l'ouverture de connexion autocommit + appel
     `pg_notify` (saturation pool / port exhaustion). Diagnostiquer
     individuellement permet de cibler le bon levier (tuning DB vs migration
     broker).
   - Origine probable : contention sur la table `outbox_events`, multi-writer,
     OU saturation des connexions autocommit éphémères du publisher.

2. **Backlog outbox** :
   - `agentive_event_bus_outbox_backlog` > **1 000** rows en moyenne sur **1 h**
   - Origine probable : worker singleton saturé OR handlers trop lents

3. **Multi-instance app** :
   - Déploiement de **≥ 2 instances backend** simultanées (passage SaaS)
   - Le worker singleton actuel ne supporte pas le partage du LISTEN — chaque
     instance pickerait les NOTIFY dupliqués → handlers exécutés N fois.

## Plan de migration en 3 phases

### Phase 1 — Dual-write (1 sprint)

- Ajouter `RedisStreamPublisher` à côté de `OutboxPublisher`.
- `publish()` écrit dans **les deux** : outbox (legacy) + stream Redis (target).
- Workers : 1 worker outbox (legacy) + 1 worker stream (nouveau) — handlers
  identiques mais idempotents (cf. `processed_at IS NULL` guard).
- Métriques de comparaison : `divergence_count` (events publiés via outbox mais
  absents de Redis ou vice-versa) → 0 pendant 7 jours = OK pour cutover.

### Phase 2 — Cutover (1 jour)

- Feature flag `use_redis_stream_bus = true` : `publish()` n'écrit plus dans
  `outbox_events`, uniquement dans Redis.
- Worker outbox legacy reste actif pour drainer le backlog résiduel < 24 h.

### Phase 3 — Decommission outbox (1 sprint)

- Vérification `SELECT COUNT(*) FROM outbox_events WHERE processed_at IS NULL = 0`
  pendant 14 jours.
- Drop du worker outbox + de la table `outbox_events` (migration Alembic).
- Suppression du code `shared.event_bus.outbox` + `publisher._outbox_insert`.

## Conséquences (positives + négatives)

**Positives** :
- Multi-instance natif (consumer groups Redis Streams).
- Streaming des events vers Trace Explorer M12 sans polling.
- Pattern matching natif côté broker (XGROUP avec wildcard).

**Négatives** :
- Nouvelle dépendance opérationnelle (Redis cluster, persistence AOF).
- Atomicité publish ↔ business write perdue si on garde une transaction Postgres
  uniquement (mitigation : keep `outbox_events` comme staging table pour un
  publisher batché vers Redis — pattern Debezium-like).

## Référence croisée

- `_bmad-output/planning-artifacts/architecture.md`, lignes 387-388 (choix
  initial), 794-800 (déclencheur), 941-943 (métriques de gating).
- `shared/event_bus/metrics.py` — métriques implémentées Story 1.4.
- ADR `001-starter-template.md` — pattern ADR de référence.
