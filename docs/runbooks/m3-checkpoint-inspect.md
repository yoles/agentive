# Runbook — Inspect M3 spike checkpoint

> **Context** : utilitaire de debug pour le spike M3 LangGraph (Story 1.2). Lit
> le dernier checkpoint persisté par :class:`AsyncPostgresSaver` pour un
> ``thread_id`` donné et imprime son contenu en JSON.

## Quand l'utiliser

- Après un `make spike-m3-crash` pour vérifier ce qui a été flushé en DB *avant*
  le SIGKILL (validation AC2 : reprise post-crash).
- Pour debugger un workflow LangGraph qui semble bloqué (le payload contient
  l'état applicatif **et** les pending writes).
- Pour mesurer la taille moyenne d'un checkpoint en bytes (mentionné dans l'ADR
  de gating `docs/decisions/m3-spike-result.md`).

## Pré-requis

- Stack dev tournant (`make up`). Le rôle `agentive_owner` doit être dispo (créé
  par `infra/postgres/init.sql`).
- Le `thread_id` recherché — récupéré soit du log du spike (`spike start
  thread_id=<uuid>`), soit du fichier `backend/.spike-thread-id` écrit avant le
  crash.

## Usage

```bash
# Via le Makefile (recommandé)
make spike-m3-inspect THREAD_ID=$(cat backend/.spike-thread-id)

# Ou en direct (équivalent)
docker compose run --rm backend uv run python -m spike.inspect_checkpoint <uuid>
```

## Exemple de sortie attendue

```json
{
  "thread_id": "9b4f1d2a-c1b3-4e2c-9c2b-1f8a7e3a5d11",
  "checkpoint_id": "1f029ca3-1f5b-6704-8004-820c16b69a5a",
  "checkpoint_values": {
    "task_input": "Write a haiku about resilience.",
    "producer_output": "[mock-producer] <user_input>Write a haiku about ...",
    "iterations": 1,
    "feedback": null,
    "approved": null
  },
  "metadata": {
    "source": "loop",
    "writes": {"producer": {"producer_output": "...", "iterations": 1}},
    "step": 1,
    "parents": {},
    "thread_id": "9b4f1d2a-c1b3-4e2c-9c2b-1f8a7e3a5d11"
  },
  "parent_config": {"configurable": {"thread_id": "...", "checkpoint_ns": "", "checkpoint_id": "..."}},
  "pending_writes_count": 0
}
```

## Troubleshooting

| Symptôme | Cause probable | Fix |
|---|---|---|
| `❌ no checkpoint found for thread_id=<uuid>` | Le workflow n'a pas atteint son premier checkpoint avant le crash, ou le `thread_id` est faux | Vérifier `cat backend/.spike-thread-id` ; relancer `make spike-m3-crash` |
| `psycopg.errors.InsufficientPrivilege: permission denied for table checkpoints` | `agentive_app` utilisé au lieu de `agentive_owner` | `_checkpoint_dsn()` doit utiliser `database_url_owner` (cf `spike/inspect_checkpoint.py`) |
| `connection refused` sur `db:5432` | Stack dev arrêté | `make up && sleep 5` puis re-essayer |
| Sortie JSON tronquée / illisible | Output piped dans un terminal sans wrapping | Rediriger vers fichier : `make spike-m3-inspect THREAD_ID=... > /tmp/cp.json && jq . /tmp/cp.json` |

## Tables LangGraph concernées (créées par `await checkpointer.setup()`)

- `checkpoints` — header + metadata du checkpoint, indexé par `(thread_id, checkpoint_id)`.
- `checkpoint_writes` — writes pending (transient).
- `checkpoint_blobs` — payloads sérialisés (msgpack interne LangGraph).

> ⚠️ **Ne pas modifier ces tables manuellement** — utilisées par LangGraph 1.x.
> Pour vider la DB de checkpoints, le plus simple est `TRUNCATE checkpoints,
> checkpoint_writes, checkpoint_blobs;` exécuté via `make db-shell`.

## Références

- [Story 1.2 AC5](../../_bmad-output/implementation-artifacts/1-2-spike-m3-langgraph.md)
- [LangGraph PostgresSaver — doc officielle](https://docs.langchain.com/oss/python/langgraph/add-memory)
