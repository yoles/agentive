"""HNSW parameter sweep — Story 1.3 gating NFR5.

**Stub Sprint 0 / Story 1.1** — implementation complète dans Story 1.3 :
    - Sweep m ∈ [8, 16, 32], ef_construction ∈ [64, 128, 256], ef_search ∈ [40, 100, 200]
    - Mesure recall@5, recall@10, latence p95, build time, taille index
    - Choix final documenté dans `docs/decisions/hnsw-tuning.md`
"""

from __future__ import annotations


def main() -> None:
    print("HNSW tuning sweep — implémentation dans Story 1.3")


if __name__ == "__main__":
    main()
