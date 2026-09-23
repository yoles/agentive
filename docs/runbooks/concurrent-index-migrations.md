# Construire un index sans `ACCESS EXCLUSIVE` (Story 4.14)

## Quand l'utiliser

Avant d'écrire une migration Alembic qui pose un index (`CREATE INDEX`/`CREATE UNIQUE INDEX`) sur une table qui peut déjà porter des lignes en production — c'est-à-dire toute table hors des tout premiers sprints. Un `CREATE INDEX` ordinaire prend un verrou `ACCESS EXCLUSIVE` sur la table pour toute la durée de la construction : plus aucune lecture ni écriture ne passe pendant ce temps.

`CREATE INDEX CONCURRENTLY` évite ce verrou (au prix de deux passes sur la table au lieu d'une, et de l'impossibilité de l'exécuter dans une transaction). C'est le choix par défaut dès qu'une table peut être non triviale en taille.

## Prérequis

- Confirmé sur ce dépôt (Story 4.14, T3.1) : `op.get_context().autocommit_block()` fonctionne **sans aucune modification** du harnais actuel (`backend/alembic/env.py`), y compris à travers le pont async→sync (`connection.run_sync(do_run_migrations)`) que ce projet utilise. Vérifié par une migration jetable réelle contre Postgres : l'index construit en `CONCURRENTLY` à l'intérieur d'un `autocommit_block()` ressort avec `pg_index.indisvalid = true`, et le `downgrade()` associé fonctionne symétriquement. Rien à outiller avant de s'en servir.

## Procédure

Dans la migration, envelopper **uniquement** la ou les instructions `CREATE INDEX CONCURRENTLY` dans `autocommit_block()` — pas le reste de la migration, qui continue de bénéficier du DDL transactionnel normal :

```python
def upgrade() -> None:
    op.add_column("ma_table", sa.Column("ma_colonne", sa.String(64), nullable=True))
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_ma_table_ma_colonne ON ma_table (ma_colonne)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_ma_table_ma_colonne")
    op.drop_column("ma_table", "ma_colonne")
```

Quatre points qui piègent facilement :

1. **`autocommit_block()` committe la transaction ambiante en entrant.** Tout ce qui précède le bloc dans la même migration est donc déjà committé et visible quand le bloc s'exécute — irréversible si le reste de la migration échoue après coup. Placer le bloc `CONCURRENTLY` en DERNIER dans `upgrade()` (et en PREMIER, symétriquement, dans `downgrade()`) minimise la fenêtre où un échec laisse la migration à moitié appliquée.

   **Et cette fenêtre n'est pas seulement « à moitié appliquée » : elle est non rejouable telle quelle.** Alembic ne stampe `alembic_version` qu'après le retour de `upgrade()`. Si le `CONCURRENTLY` échoue, le DDL qui le précédait est committé mais la révision reste enregistrée comme non appliquée : un `alembic upgrade head` rejoue `upgrade()` depuis le début et meurt sur `DuplicateColumn` (ou équivalent), et `downgrade()` est inatteignable puisqu'on n'est pas à cette révision. La migration est alors bloquée dans les deux sens, et il faut réparer à la main.

   Deux façons de ne pas s'y exposer, par ordre de préférence :
   - **une révision qui ne contient QUE le bloc `CONCURRENTLY`**, et le DDL transactionnel dans la révision précédente. Rien à rejouer partiellement, rien à rendre idempotent ;
   - à défaut, **rendre idempotent tout DDL placé avant le bloc** (`IF NOT EXISTS`, `IF EXISTS`) — c'est ce que font déjà `20260913_000001` et `20260913_000002` de ce dépôt, dont les blocs ouvrent par un `DROP INDEX CONCURRENTLY IF EXISTS` avant de reconstruire, précisément pour être rejouables après un échec.
2. **Déclarer l'index dans `Base.metadata` ne protège PAS de l'`--autogenerate`** *(Story 4.15 AC4, correction d'une revendication de la Story 4.8 P4)*. La déclaration reste nécessaire — `Base.metadata` doit décrire le schéma que l'application attend — mais elle n'achète pas la protection qu'on lui prêtait. Alembic ne compare pas de façon fiable les index d'**expression** ni les prédicats `postgresql_where` contre la base réfléchie : l'issue habituelle n'est pas « aucun diff » mais une paire `drop_index`/`create_index` parasite, émise **sans** `CONCURRENTLY`, donc un `ACCESS EXCLUSIVE` pendant toute la reconstruction — exactement ce que ce runbook existe pour éviter.

   **La vraie protection est procédurale** : toute migration autogénérée qui touche `ix_workflow_runs_stale_running`, `ix_workflow_runs_workflow_started` ou `uq_workflow_request_fingerprint` est relue à la main avant d'être appliquée, et un `drop_index`/`create_index` parasite sur l'un d'eux est **supprimé de la migration**, pas exécuté. Il n'existe pas de garde automatique pour ça aujourd'hui ; le savoir est ici.
3. **`DROP INDEX CONCURRENTLY` existe aussi** et doit être utilisé dans `downgrade()` par symétrie, pour la même raison que la construction : un `DROP INDEX` ordinaire prend lui aussi `ACCESS EXCLUSIVE`.
4. **Le coût d'un `ACCESS EXCLUSIVE`, ce n'est pas seulement sa détention — c'est aussi son acquisition**, et cette moitié-là n'est bornée par aucune mesure de durée de construction. Un `CREATE INDEX` ordinaire doit attendre la fin de toute transaction conflictuelle sur la table, et **pendant qu'il fait la queue, il bloque tout lecteur et tout écrivain qui arrive derrière lui**. Une migration dont la construction dure 46 ms peut donc immobiliser la table plusieurs minutes si une transaction longue (ou un `idle in transaction`) la précède. C'est indépendant de `CONCURRENTLY` : cela vaut pour tout DDL transactionnel. Poser un `SET LOCAL lock_timeout` sur la session de migration et réessayer est la parade — la Story 4.11 T7.1 l'a fait pour le DDL de `workflow_runs`, avec un test (`backend/tests/unit/workflow_engine/test_control_signal_migration.py`) ; suivre ce précédent plutôt que de le redécouvrir.

## Rollback

`alembic downgrade -1` exécute le `downgrade()` de la migration, qui doit lui-même utiliser `DROP INDEX CONCURRENTLY` dans son propre `autocommit_block()` (voir l'exemple ci-dessus) pour ne pas réintroduire le problème que la migration cherchait à éviter.

## Vérifications

- `pg_index.indisvalid` doit valoir `true` pour l'index construit — un `CREATE INDEX CONCURRENTLY` qui échoue à mi-chemin (ex. contrainte violée par une ligne existante) laisse un index **invalide** qui occupe de l'espace et doit être `DROP`é manuellement avant de rejouer la migration ; Postgres ne le refait jamais tout seul.
- **`alembic_version` doit être cohérent avec ce qui a réellement été appliqué.** Après un échec dans le bloc, vérifier la révision courante (`alembic current`) ET l'état du DDL committé avant le bloc (colonne créée ? contrainte posée ?). Si la révision n'a pas été stampée alors que le DDL pré-bloc existe, la migration n'est pas rejouable en l'état — cf le point 1 des pièges pour les deux façons de l'éviter.
- `EXPLAIN` une requête qui devrait utiliser le nouvel index, pour confirmer que le planificateur le voit et l'emploie.

## Référence

- Mesure réelle sur ce dépôt (Story 4.14 T3.2), pour dimensionner quand `CONCURRENTLY` est nécessaire plutôt qu'une précaution : un `CREATE UNIQUE INDEX` ordinaire (non-`CONCURRENTLY`) sur `workflows (request_fingerprint, tenant_id)`, prédicat partiel ne matchant aucune row, a pris **13,6 ms sur 100 000 lignes** et **46,7 ms sur 1 000 000 lignes** — négligeable, parce que le prédicat partiel réduit le travail à un balayage de table plutôt qu'à une écriture d'index par ligne. ⚠️ **Ces chiffres mesurent la détention du verrou, jamais son acquisition** (cf piège 4 ci-dessus) : ils ne disent rien du temps passé à l'obtenir derrière une transaction longue, pendant lequel la table est de fait indisponible. Ce n'est PAS un plancher universel : un index **non partiel**, ou dont le prédicat matche une fraction significative des lignes, paie le coût d'écriture complet et doit être mesuré au cas par cas avant de décider si `CONCURRENTLY` est nécessaire.
