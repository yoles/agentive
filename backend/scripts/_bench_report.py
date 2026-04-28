"""Génère `docs/decisions/hnsw-tuning.md` à partir des CSV produits par les bench scripts.

Lit en entrée :
    - `_bench_artifacts/baseline_results.csv` (1 ligne, sortie de benchmark_m4)
    - `_bench_artifacts/hnsw_results.csv` (8 ou 27 lignes, sortie de benchmark_hnsw)
    - `_bench_artifacts/ivfflat_results.csv` (9 lignes, sortie de benchmark_ivfflat)

Sortie : `docs/decisions/hnsw-tuning.md` avec :
    - Tableau HNSW trié par latence p95 ascendante
    - Tableau ivfflat trié par latence p95 ascendante
    - Heatmap textuelle (m x ef_construction) à ef_search=100
    - Configuration retenue (sélection auto = lowest p95 parmi recall@5 >= NFR4
      ; tie-break baseline si delta p95 < 1.5ms — cf P6 review)
    - Recommandation `ef_search` runtime configurable
    - Cible NFR4/NFR5 atteinte ou non + verdict GO/NO-GO

Si aucune config HNSW ne satisfait NFR4 (recall >= 0.9), section ÉCHEC + suggestion AC7.

Exit codes :
    - 0 si rapport généré avec données HNSW présentes
    - 1 si `hnsw_results.csv` absent (P2 review : signal CI clair) ou si CSV malformée
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

BENCH_ARTIFACTS_DIR = Path("_bench_artifacts")
# Container WORKDIR=/app (= host backend/), so we write to _bench_artifacts/ which is
# shared via the bind mount. The Makefile target copies the file to docs/decisions/
# on the host post-run (the docs/ folder isn't mounted in the container).
OUTPUT_PATH = BENCH_ARTIFACTS_DIR / "hnsw-tuning.md"
# M3 review : utiliser >= pour matcher exactement la formulation NFR4 ("recall@5 > 90%").
# Avec strict >, une mesure recall=0.9000 est rejetée alors qu'elle satisfait l'AC.
NFR4_THRESHOLD = 0.90
NFR5_THRESHOLD_MS = 200.0
# P6 review : tolérance pour le tie-break baseline. Si la meilleure p95 mesurée
# est à moins de cette marge de la p95 baseline (m=16, ef_c=64), on conserve le
# baseline pour éviter une migration Alembic inutile sur du bruit de mesure.
TIE_BREAK_P95_MS = 1.5
BASELINE_M = 16
BASELINE_EF_CONSTRUCTION = 64


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float(s: str) -> float:
    return float(s)


def to_int(s: str) -> int:
    return int(float(s))


def safe_to_float(row: dict[str, str], key: str) -> float | None:
    """M8 review : robustness face à une CSV malformée (clé manquante, valeur vide,
    valeur non-numérique). Retourne None pour signaler un row à skipper, sans
    faire crasher la génération du rapport."""
    val = row.get(key)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def effective_recall_for_nfr4(row: dict[str, str]) -> float:
    """P7 review : NFR4 cible le **recall de l'index ANN** (sa capacité à ramener
    les top-K pertinents). Quand `recall_at_5_strict` (top_k_ann == k) est mesuré,
    il représente cette quantité directement. Le `recall_at_5` "regular"
    (top_k_ann=50 + rerank) mélange la sélectivité de l'ANN avec la qualité du
    rerank — sur embeddings synthétiques avec `recency_decay` non-corrélé au
    contenu, le rerank dégrade fortement le score combiné, ce qui n'est PAS un
    signal pour NFR4 (et ne représente pas le runtime applicatif réel qui
    utilisera un cross-encoder).

    On préfère donc `recall_at_5_strict` quand présent, fallback sur `recall_at_5`
    pour rétrocompatibilité avec les CSV pré-P7.
    """
    strict = safe_to_float(row, "recall_at_5_strict")
    if strict is not None:
        return strict
    return float(row["recall_at_5"])


def filter_well_formed(
    rows: list[dict[str, str]], required_keys: list[str]
) -> list[dict[str, str]]:
    """M8 review : filtre les rows ayant au moins toutes les clés `required_keys`
    parsables en float. Affiche un warning pour chaque row ignoré."""
    out: list[dict[str, str]] = []
    for r in rows:
        if all(safe_to_float(r, k) is not None for k in required_keys):
            out.append(r)
        else:
            print(f"⚠️ skipping malformed row: {r}", file=sys.stderr)
    return out


def render_hnsw_table(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "_(aucune donnée HNSW — exécuter `make bench-hnsw` ou `make bench-hnsw-fast`)_\n"
    sorted_rows = sorted(rows, key=lambda r: to_float(r["latency_p95_ms"]))
    has_strict = any(r.get("recall_at_5_strict") not in (None, "") for r in sorted_rows)
    if has_strict:
        lines = [
            "| m | ef_c | ef_s | build_s | size_MB | recall@5 | recall@5_strict | recall@10 | p50_ms | p95_ms | NFR |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    else:
        lines = [
            "| m | ef_c | ef_s | build_s | size_MB | recall@5 | recall@10 | p50_ms | p95_ms | NFR |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    for r in sorted_rows:
        # P7 review : verdict NFR4 utilise recall_at_5_strict quand présent
        # (sélectivité ANN sans confounding du rerank).
        recall_for_nfr4 = effective_recall_for_nfr4(r)
        p95 = to_float(r["latency_p95_ms"])
        # M3 review : `recall >= NFR4_THRESHOLD` (PRD dit "> 90%" mais une mesure
        # exacte à 0.9000 est conforme — strict > rejette artificiellement le seuil).
        verdict = "✅" if (recall_for_nfr4 >= NFR4_THRESHOLD and p95 < NFR5_THRESHOLD_MS) else "❌"
        if has_strict:
            strict_val = r.get("recall_at_5_strict") or "—"
            lines.append(
                f"| {r['m']} | {r['ef_construction']} | {r['ef_search']} | "
                f"{r['build_time_s']} | {r['index_size_mb']} | "
                f"{r['recall_at_5']} | {strict_val} | {r['recall_at_10']} | "
                f"{r['latency_p50_ms']} | {r['latency_p95_ms']} | {verdict} |"
            )
        else:
            lines.append(
                f"| {r['m']} | {r['ef_construction']} | {r['ef_search']} | "
                f"{r['build_time_s']} | {r['index_size_mb']} | "
                f"{r['recall_at_5']} | {r['recall_at_10']} | "
                f"{r['latency_p50_ms']} | {r['latency_p95_ms']} | {verdict} |"
            )
    return "\n".join(lines) + "\n"


def render_ivfflat_table(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "_(aucune donnée ivfflat — exécuter `make bench-ivfflat`)_\n"
    sorted_rows = sorted(rows, key=lambda r: to_float(r["latency_p95_ms"]))
    has_strict = any(r.get("recall_at_5_strict") not in (None, "") for r in sorted_rows)
    if has_strict:
        lines = [
            "| lists | probes | build_s | size_MB | recall@5 | recall@5_strict | recall@10 | p50_ms | p95_ms | NFR |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    else:
        lines = [
            "| lists | probes | build_s | size_MB | recall@5 | recall@10 | p50_ms | p95_ms | NFR |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    for r in sorted_rows:
        recall_for_nfr4 = effective_recall_for_nfr4(r)
        p95 = to_float(r["latency_p95_ms"])
        verdict = "✅" if (recall_for_nfr4 >= NFR4_THRESHOLD and p95 < NFR5_THRESHOLD_MS) else "❌"
        if has_strict:
            strict_val = r.get("recall_at_5_strict") or "—"
            lines.append(
                f"| {r['lists']} | {r['probes']} | {r['build_time_s']} | {r['index_size_mb']} | "
                f"{r['recall_at_5']} | {strict_val} | {r['recall_at_10']} | "
                f"{r['latency_p50_ms']} | {r['latency_p95_ms']} | {verdict} |"
            )
        else:
            lines.append(
                f"| {r['lists']} | {r['probes']} | {r['build_time_s']} | {r['index_size_mb']} | "
                f"{r['recall_at_5']} | {r['recall_at_10']} | "
                f"{r['latency_p50_ms']} | {r['latency_p95_ms']} | {verdict} |"
            )
    return "\n".join(lines) + "\n"


def render_heatmap(rows: list[dict[str, str]], ef_search: int = 100) -> str:
    """Heatmap textuelle (m x ef_construction) à ef_search fixé."""
    rows_at_ef = [r for r in rows if to_int(r["ef_search"]) == ef_search]
    if not rows_at_ef:
        return f"_(aucune donnée à ef_search={ef_search})_\n"

    m_values = sorted({to_int(r["m"]) for r in rows_at_ef})
    ef_c_values = sorted({to_int(r["ef_construction"]) for r in rows_at_ef})

    by_key: dict[tuple[int, int], dict[str, str]] = {
        (to_int(r["m"]), to_int(r["ef_construction"])): r for r in rows_at_ef
    }

    header = "| m \\ ef_c | " + " | ".join(str(c) for c in ef_c_values) + " |"
    sep = "|---|" + "|".join(["---"] * len(ef_c_values)) + "|"
    lines = [header, sep]
    for m in m_values:
        cells = []
        for c in ef_c_values:
            r = by_key.get((m, c))
            if r is None:
                cells.append("—")
                continue
            recall = effective_recall_for_nfr4(r)
            recall_display = (
                to_float(r["recall_at_5_strict"])
                if r.get("recall_at_5_strict") not in (None, "")
                else to_float(r["recall_at_5"])
            )
            p95 = to_float(r["latency_p95_ms"])
            ok = "✅" if recall >= NFR4_THRESHOLD else "❌"
            cells.append(f"{p95:.1f}ms / r5={recall_display:.2f} {ok}")
        lines.append(f"| **{m}** | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def select_best_config(rows: list[dict[str, str]]) -> dict[str, str] | None:
    """P6 review : tie-break baseline.

    Logique de sélection (en deux passes) :
    1. Filtrer les configs satisfaisant recall@5 >= NFR4 ET p95 < NFR5.
    2. Si la baseline `(m=16, ef_c=64)` est dans le set passant et que sa p95 est
       à moins de TIE_BREAK_P95_MS du minimum mesuré, retourner la baseline (en
       préférant ef_search=100 — config historique).
    3. Sinon, retourner la config avec la plus basse p95.

    Justification (P6 review) : sur 10k chunks, les écarts p95 sont typiquement
    dans le bruit de mesure (~1ms). Recommander une migration Alembic d'ajustement
    sur cette base contredit l'ADR `m4-bench-result.md` qui privilégie la stabilité
    quand le gain est non-significatif.
    """
    passing = [
        r
        for r in rows
        if effective_recall_for_nfr4(r) >= NFR4_THRESHOLD
        and to_float(r["latency_p95_ms"]) < NFR5_THRESHOLD_MS
    ]
    if not passing:
        return None

    best = min(passing, key=lambda r: to_float(r["latency_p95_ms"]))
    best_p95 = to_float(best["latency_p95_ms"])

    # Tie-break baseline n'a de sens que pour HNSW (les rows ivfflat n'ont pas
    # de colonnes `m` / `ef_construction`). Si les clés sont absentes, on skip
    # le tie-break et on retourne directement la meilleure p95.
    if not all("m" in r and "ef_construction" in r for r in passing):
        return best
    baseline_candidates = [
        r
        for r in passing
        if to_int(r["m"]) == BASELINE_M and to_int(r["ef_construction"]) == BASELINE_EF_CONSTRUCTION
    ]
    if not baseline_candidates:
        return best
    baseline_best = min(baseline_candidates, key=lambda r: abs(to_int(r["ef_search"]) - 100))
    baseline_p95 = to_float(baseline_best["latency_p95_ms"])
    if (baseline_p95 - best_p95) <= TIE_BREAK_P95_MS:
        return baseline_best
    return best


def render_chosen_config_section(
    hnsw_rows: list[dict[str, str]], ivfflat_rows: list[dict[str, str]]
) -> str:
    best_hnsw = select_best_config(hnsw_rows)
    best_ivfflat = select_best_config(ivfflat_rows)

    if best_hnsw is None and best_ivfflat is None:
        return (
            "## ❌ ÉCHEC NFR4/NFR5\n\n"
            f"**Aucune configuration testée ne satisfait simultanément recall@5 >= {NFR4_THRESHOLD} "
            f"et p95 < {NFR5_THRESHOLD_MS}ms.**\n\n"
            "→ Déclencher AC7 (tuning Postgres : shared_buffers 512MB, work_mem 32MB, "
            "maintenance_work_mem 256MB) puis re-run du sweep. Si NFR5 reste violé, "
            "produire l'ADR de pivot `m4-bench-result.md` avec verdict NO-GO.\n"
        )

    if best_hnsw is None:
        return (
            "## ⚠️ HNSW ne satisfait pas NFR4/NFR5 — fallback ivfflat\n\n"
            f"**Configuration ivfflat retenue** : `lists={best_ivfflat['lists']}, "  # type: ignore[index]
            f"probes={best_ivfflat['probes']}` "  # type: ignore[index]
            f"⇒ recall@5={best_ivfflat['recall_at_5']}, p95={best_ivfflat['latency_p95_ms']}ms.\n\n"  # type: ignore[index]
            "Décision à prendre : fallback ivfflat (recall plafonné par probes/lists) "
            "OU pivot ADR (Qdrant/Weaviate sidecar — cf Architecture ligne 843).\n"
        )

    m = best_hnsw["m"]
    ef_c = best_hnsw["ef_construction"]
    ef_s = best_hnsw["ef_search"]
    # P7 review : afficher strict recall en premier (signal NFR4) puis le recall
    # post-rerank pour transparence sur l'écart.
    recall_strict = best_hnsw.get("recall_at_5_strict") or best_hnsw["recall_at_5"]
    recall_post_rerank = best_hnsw["recall_at_5"]
    p95 = best_hnsw["latency_p95_ms"]
    build = best_hnsw["build_time_s"]
    size = best_hnsw["index_size_mb"]

    baseline_match = (to_int(m) == BASELINE_M) and (to_int(ef_c) == BASELINE_EF_CONSTRUCTION)
    migration_note = (
        "**Pas de migration appliquée** — les params baseline "
        f"(m={BASELINE_M}, ef_construction={BASELINE_EF_CONSTRUCTION}) sont confirmés "
        f"optimaux par le benchmark (avec tie-break ≤ {TIE_BREAK_P95_MS}ms : si une config "
        "alternative est <= 1.5ms plus rapide, on conserve baseline car ce delta est "
        "dans le bruit de mesure)."
        if baseline_match
        else (
            f"⚠️ Params optimaux ({m}, {ef_c}) ≠ baseline ({BASELINE_M}, {BASELINE_EF_CONSTRUCTION}) "
            f"avec un écart p95 > {TIE_BREAK_P95_MS}ms ⇒ une migration Alembic "
            f"`adjust_hnsw_params` doit être créée (cf Story 1.3 AC6/T7)."
        )
    )

    return (
        "## ✅ Configuration retenue\n\n"
        f"**`m={m}, ef_construction={ef_c}, ef_search={ef_s}`** ⇒ "
        f"**recall@5_strict={recall_strict}** (sélectivité ANN, signal NFR4), "
        f"recall@5={recall_post_rerank} (post-rerank, informationnel), "
        f"p95={p95}ms, build={build}s, taille={size}MB.\n\n"
        "**Justification** : sélection en deux passes — (1) plus basse p95 parmi les configs "
        f"satisfaisant simultanément recall@5_strict >= {NFR4_THRESHOLD} (cf P7 review : "
        "le `recall_at_5_strict` est le signal NFR4 ; le `recall_at_5` post-rerank est "
        f"confondé sur synthétique) et p95 < {NFR5_THRESHOLD_MS}ms (NFR5) ; (2) tie-break "
        f"baseline si l'écart p95 avec la baseline (m={BASELINE_M}, ef_c={BASELINE_EF_CONSTRUCTION}) "
        f"est ≤ {TIE_BREAK_P95_MS}ms (P6 review : éviter les migrations sur du bruit de mesure).\n\n"
        f"{migration_note}\n\n"
        "**`ef_search` runtime** : valeur retenue `100` par défaut. Override possible "
        "via `SET LOCAL hnsw.ef_search = 200` pour les requêtes haute qualité "
        "(Vision/Cross-Pollinator Story 7.6) — coût ~2x latence.\n"
    )


REQUIRED_HNSW_KEYS = [
    "recall_at_5",
    "latency_p95_ms",
    "ef_search",
    "m",
    "ef_construction",
]
REQUIRED_IVFFLAT_KEYS = [
    "recall_at_5",
    "latency_p95_ms",
    "lists",
    "probes",
]


def render_report() -> str:
    hnsw_rows_raw = read_csv(BENCH_ARTIFACTS_DIR / "hnsw_results.csv")
    ivfflat_rows_raw = read_csv(BENCH_ARTIFACTS_DIR / "ivfflat_results.csv")
    baseline_rows = read_csv(BENCH_ARTIFACTS_DIR / "baseline_results.csv")
    # M8 review : un row manquant des clés numériques requises est skippé avec
    # un warning sur stderr — la génération du rapport reste robuste.
    hnsw_rows = filter_well_formed(hnsw_rows_raw, REQUIRED_HNSW_KEYS)
    ivfflat_rows = filter_well_formed(ivfflat_rows_raw, REQUIRED_IVFFLAT_KEYS)

    parts: list[str] = []
    parts.append("# HNSW Tuning Report — Story 1.3 (Benchmark M4 pgvector)\n")
    parts.append(
        "> **Auto-généré** par `scripts/_bench_report.py` à partir des CSV "
        "`_bench_artifacts/*.csv`. Ne pas éditer à la main : pour modifier, ré-exécuter "
        "`make bench-report` après un nouveau sweep.\n"
    )
    parts.append(
        f"**Cible NFR4** : recall@5_strict (top_k_ann == k=5) >= {NFR4_THRESHOLD} sur "
        "50 requêtes validation (synthétiques unit-norm 384 dims). Le verdict "
        "`recall_at_5_strict` mesure la sélectivité de l'index ANN sans confounding "
        "du rerank Python placeholder. La colonne `recall@5` (top_k_ann=50 + rerank) "
        "est informationnelle — sur ce dataset elle reflète la qualité du scoring "
        "rerank, pas celle de l'index.\n"
    )
    parts.append(
        f"**Cible NFR5** : p95 latency < {NFR5_THRESHOLD_MS}ms end-to-end "
        f"(ANN top-50 + reranking Python placeholder).\n\n"
    )
    parts.append(
        "**Dataset** : 10k chunks synthétiques, modèle `bge-small-en-v1.5` (384 dims), "
        "vecteurs aléatoires unit-norm, namespace `bench-m4-synthetic`. "
        "`created_at` varié uniformément sur `[now-90j, now]` (P7 review : rend `recency_decay` "
        "non-constant et le rerank discriminant — sinon dégénère en re-tri cosine).\n\n"
    )
    parts.append(
        "**Limitation** : embeddings synthétiques ≠ sémantique réelle. Le `recall_at_5_strict` "
        "mesure la fidélité de l'index ANN vs brute-force exact (top-K) ⇒ valide pour NFR4. "
        "Le `recall_at_5` post-rerank reflète l'apport du scoring 0.7*cosine + 0.3*recency_decay, "
        "qui sur du synthétique non-corrélé recency↔contenu dégrade fortement le ranking — "
        "non représentatif du runtime applicatif (cross-encoder Story 3.6). La qualité "
        "sémantique réelle sera mesurée Story 3.1 / 7.6.\n\n"
    )

    if baseline_rows:
        parts.append("## Baseline (m=16, ef_construction=64, ef_search=100)\n\n")
        b = baseline_rows[0]
        parts.append(
            f"- recall@5 = **{b['recall_at_5']}**\n"
            f"- recall@10 = {b['recall_at_10']}\n"
            f"- p50 = {b['latency_p50_ms']}ms / p95 = **{b['latency_p95_ms']}ms**\n"
            f"- build_time = {b['build_time_s']}s, index_size = {b['index_size_mb']}MB\n\n"
        )

    parts.append("## Sweep HNSW — résultats triés par p95 ascendante\n\n")
    parts.append(render_hnsw_table(hnsw_rows))
    parts.append("\n")

    parts.append("## Heatmap (m x ef_construction) à ef_search=100\n\n")
    parts.append("Format cellule : `p95_ms / r5=recall@5 ✅(NFR4)❌`\n\n")
    parts.append(render_heatmap(hnsw_rows, ef_search=100))
    parts.append("\n")

    parts.append("## Sweep ivfflat — comparaison\n\n")
    parts.append(render_ivfflat_table(ivfflat_rows))
    parts.append("\n")

    parts.append(render_chosen_config_section(hnsw_rows, ivfflat_rows))
    parts.append("\n")

    parts.append("## Future improvements\n\n")
    parts.append(
        "- **`halfvec`** (pgvector 0.7+) : quantization 16-bit float ⇒ ~50% taille index, "
        "recall stable. À évaluer Story 7.6 / Sprint 5+.\n"
        "- **Bench sur 100k chunks réels** (anticipation H6, Architecture ligne 871) : "
        "embeddings via OpenAI / `voyage-3-lite` sur dataset OpenAssistant FR + Wikipedia EN. "
        "À planifier avant Sprint 4 (Growth phase).\n"
        "- **Bench dimension 1536** (`text-embedding-3-small`) : extrapoler depuis le "
        "sweep 384 dims — full sweep dédié si stories Epic 3 montrent un usage cloud "
        "dominant.\n"
        "- **Concurrence + pool de connections** : tester N_CONCURRENT x N_QUERIES (Story 3.1).\n"
    )

    return "".join(parts)


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = render_report()
    OUTPUT_PATH.write_text(report, encoding="utf-8")
    print(f"✅ Report generated: {OUTPUT_PATH}")
    # P2 review : exit code non-zero clair si la pipeline est cassée (CI signal).
    # Sans `sys.exit(1)`, CI passait au vert même sans données HNSW.
    if not (BENCH_ARTIFACTS_DIR / "hnsw_results.csv").exists():
        print(
            "⚠️ Aucun résultat HNSW trouvé. Exécuter `make bench-hnsw-fast` d'abord.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
