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

Trois points qui piègent facilement :

1. **`autocommit_block()` committe la transaction ambiante en entrant.** Tout ce qui précède le bloc dans la même migration est donc déjà committé et visible quand le bloc s'exécute — irréversible si le reste de la migration échoue après coup. Placer le bloc `CONCURRENTLY` en DERNIER dans `upgrade()` (et en PREMIER, symétriquement, dans `downgrade()`) minimise la fenêtre où un échec laisse la migration à moitié appliquée.
2. **Pas d'index unique `NULLS NOT DISTINCT` sans le déclarer aussi dans les modèles** (cf `docs/decisions/workflow-creation-idempotency.md` § index partiel) — la remarque de la Story 4.8 P4 s'applique identiquement ici : sinon le prochain `alembic revision --autogenerate` propose un `DROP INDEX`.
3. **`DROP INDEX CONCURRENTLY` existe aussi** et doit être utilisé dans `downgrade()` par symétrie, pour la même raison que la construction : un `DROP INDEX` ordinaire prend lui aussi `ACCESS EXCLUSIVE`.

## Rollback

`alembic downgrade -1` exécute le `downgrade()` de la migration, qui doit lui-même utiliser `DROP INDEX CONCURRENTLY` dans son propre `autocommit_block()` (voir l'exemple ci-dessus) pour ne pas réintroduire le problème que la migration cherchait à éviter.

## Vérifications

- `pg_index.indisvalid` doit valoir `true` pour l'index construit — un `CREATE INDEX CONCURRENTLY` qui échoue à mi-chemin (ex. contrainte violée par une ligne existante) laisse un index **invalide** qui occupe de l'espace et doit être `DROP`é manuellement avant de rejouer la migration ; Postgres ne le refait jamais tout seul.
- `EXPLAIN` une requête qui devrait utiliser le nouvel index, pour confirmer que le planificateur le voit et l'emploie.

## Référence

- Mesure réelle sur ce dépôt (Story 4.14 T3.2), pour dimensionner quand `CONCURRENTLY` est nécessaire plutôt qu'une précaution : un `CREATE UNIQUE INDEX` ordinaire (non-`CONCURRENTLY`) sur `workflows (request_fingerprint, tenant_id)`, prédicat partiel ne matchant aucune row, a pris **13,6 ms sur 100 000 lignes** et **46,7 ms sur 1 000 000 lignes** — négligeable, parce que le prédicat partiel réduit le travail à un balayage de table plutôt qu'à une écriture d'index par ligne. Ce n'est PAS un plancher universel : un index **non partiel**, ou dont le prédicat matche une fraction significative des lignes, paie le coût d'écriture complet et doit être mesuré au cas par cas avant de décider si `CONCURRENTLY` est nécessaire.
