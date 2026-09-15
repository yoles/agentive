"""Centralized configuration via Pydantic Settings.

CRITICAL: This is the ONLY module allowed to read from os.environ / .env.
All other modules must import `settings` from here.
"""

from __future__ import annotations

import posixpath
from decimal import Decimal
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote_plus

from cryptography.fernet import Fernet, InvalidToken
from pydantic import (
    Field,
    PostgresDsn,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel strings used in `.env.example` — any match forbids production use.
_DEV_DEFAULT_SENTINELS: tuple[str, ...] = (
    "change_me",
    "change_me_dev_token",
    "change_me_fernet_key",
    "change_me_app_dev",
    "change_me_owner_dev",
    "change_me_audit_dev",
)


def _normalise_root(root: str) -> str:
    """Le chemin tel qu'il sera réellement appliqué.

    `posixpath.normpath` retire les `//`, les `.`, les `/` finaux et résout les
    `..` **textuellement** (sans toucher au disque, donc sans suivre de
    symlink : c'est le serveur qui refuse une racine non canonique).
    """
    return posixpath.normpath(root)


class Settings(BaseSettings):
    """Application configuration — loaded from environment variables.

    See `.env.example` at the repo root for documented variables.

    Production safety: model validators below refuse to boot with any
    ``change_me*`` placeholder if ``NODE_ENV == "production"`` to fail fast
    instead of silently running with dev credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Runtime ───
    environment: Literal["development", "production", "test"] = Field(
        default="development", alias="NODE_ENV"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    # ─── Auth (Story 1.7) ───
    agentive_api_token: SecretStr = Field(
        default=SecretStr("change_me"), alias="AGENTIVE_API_TOKEN"
    )

    # ─── Encryption (Story 9.2) ───
    agentive_encryption_key: SecretStr = Field(
        default=SecretStr("change_me"), alias="AGENTIVE_ENCRYPTION_KEY"
    )
    # Key-rotation window only (Story 9.2 AC3): set alongside a new
    # AGENTIVE_ENCRYPTION_KEY so `shared.security.crypto` can still decrypt
    # rows written under the outgoing key while `scripts/rotate_encryption_key.py`
    # re-encrypts them under the new one. Unset once the rotation script confirms
    # completion — never required outside of an active rotation.
    agentive_encryption_key_previous: SecretStr | None = Field(
        default=None, alias="AGENTIVE_ENCRYPTION_KEY_PREVIOUS"
    )

    # ─── PostgreSQL (components — the DSN is built app-side via `computed_field`) ───
    # We store the components rather than a full DSN so the password can be
    # URL-encoded when needed (supports `@`, `/`, `:`, `%`, etc.).
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="agentive", alias="POSTGRES_DB")
    postgres_app_password: SecretStr = Field(
        default=SecretStr("change_me"), alias="POSTGRES_APP_PASSWORD"
    )
    postgres_owner_password: SecretStr = Field(
        default=SecretStr("change_me"), alias="POSTGRES_OWNER_PASSWORD"
    )

    # ─── LLM Providers (Stories 1.6 + 9.3) ───
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    voyage_api_key: SecretStr | None = Field(default=None, alias="VOYAGE_API_KEY")

    # ─── MCP Tool Hub (Story 2.5 — P-23 admin-gate) ───
    # Opt-in flag: `POST /tools/servers` accepts user-supplied `command` (stdio
    # subprocess) or `url` (SSE) without sandbox or URL filtering. This is RCE
    # and SSRF surface by design — sandbox bwrap arrives Story 2.6 (D59), URL
    # allowlist Story 4.x (D60). Flip to `true` only in dev/test or after
    # Story 2.6. Default `false` = the endpoint returns 403.
    mcp_allow_registration: bool = Field(default=False, alias="AGENTIVE_ALLOW_MCP_REGISTRATION")

    # ─── Workflow Engine — hybrid routing (Story 4.3) ───
    # Deployment-level tuning for `engine/hybrid_router.py`'s DSL → rules →
    # LLM escalation sequence. Lives in `Settings`, not the rules YAML
    # catalog (T1.3) — two sources of truth for the same setting is the
    # motif already corrected three times in this repo (D84, `config.llm`
    # fantôme de 3.5, `embedding_backend`).
    # `allow_inf_nan=False` is NOT decorative on these two floats: `ge`/`le`/`gt`
    # do not reject NaN (every comparison against NaN is false, so no bound
    # ever trips). A `NaN` threshold would boot cleanly and then make
    # `match.confidence >= threshold` false forever — every decision escalating
    # to the LLM, silently, with no error to trace. Same guard as
    # `routing_catalog._RuleModel` applies to the catalog's own floats (T3.3).
    routing_confidence_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        alias="AGENTIVE_ROUTING_CONFIDENCE_THRESHOLD",
    )
    routing_escalation_model: str = Field(
        default="claude-haiku-4-5", min_length=1, alias="AGENTIVE_ROUTING_ESCALATION_MODEL"
    )
    # `le=60.0` added by Story 4.6's review (lot 7, P-P). This field was the
    # only unbounded term of `recovery.derive_stale_threshold_s`, and it is
    # multiplied there by chain length x attempts x margin — a factor of 20,
    # so every second granted here costs twenty before a crashed run is even
    # looked at. 60.0 is `agent_node.NODE_TIMEOUT_S`, the budget of a node's
    # own full LLM call: an escalation is a single classification returning
    # at most `routing_escalation_max_tokens` (256 by default), so it has no
    # business outlasting the node it routes.
    routing_escalation_timeout_s: float = Field(
        default=15.0,
        gt=0.0,
        le=60.0,
        allow_inf_nan=False,
        alias="AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S",
    )
    routing_escalation_max_tokens: int = Field(
        default=256, ge=1, le=4096, alias="AGENTIVE_ROUTING_ESCALATION_MAX_TOKENS"
    )

    # ─── Workflow Engine — Dry Run predictif (Story 4.4) ───
    # Deployment-level tuning for `dry_run.py`'s estimation. Same posture as
    # the `AGENTIVE_ROUTING_*` block above: `Settings` is the single source
    # of truth, never a `config.yaml`-style secondary source (D84 lesson,
    # applied again).
    dry_run_history_limit: int = Field(
        default=20, ge=1, le=100, alias="AGENTIVE_DRY_RUN_HISTORY_LIMIT"
    )
    # `le` is not decorative: these two feed `Decimal(tokens) / 1_000_000 *
    # price`, then `.quantize(Decimal("0.000001"))`. Past ~1e22 tokens that
    # quantize exceeds the default decimal context's 28-digit precision and
    # raises `InvalidOperation` — a 500 on every priced Dry Run, caused by an
    # env var. A ceiling two orders of magnitude above the largest real
    # context window keeps the arithmetic in range while still allowing any
    # plausible tuning (review fix P1/P2).
    dry_run_fallback_input_tokens: int = Field(
        default=500, ge=1, le=100_000_000, alias="AGENTIVE_DRY_RUN_FALLBACK_INPUT_TOKENS"
    )
    dry_run_fallback_output_tokens: int = Field(
        default=500, ge=1, le=100_000_000, alias="AGENTIVE_DRY_RUN_FALLBACK_OUTPUT_TOKENS"
    )
    # `None` (default) disables the check entirely — a Sprint-1 safety net,
    # not Story 9.4's real per-department/per-workflow budget caps (see
    # Story 4.4 Dev Notes § Budget cap).
    #
    # Three guards, each earned by a review finding (P1):
    # * `_blank_budget_cap_to_none` below — `AGENTIVE_DRY_RUN_BUDGET_CAP_USD=`
    #   (the natural way to "turn it off" in a copied `.env`) parsed as `""`,
    #   which is neither a valid `Decimal` nor `None`. `Settings` then failed
    #   to instantiate at import time, taking the WHOLE backend down over an
    #   optional knob. `.env.example` documented "vide = désactivé"; now it
    #   is true.
    # * `ge=0` — a negative cap is below every possible estimate, so
    #   `budget_cap_exceeded` fires on every workflow forever: the risk
    #   becomes permanent noise and stops being a signal.
    # * `allow_inf_nan=False` — same reasoning as
    #   `routing_confidence_threshold` above, but worse here: `ge` does not
    #   reject NaN, and unlike a float comparison, `Decimal('1') >
    #   Decimal('NaN')` RAISES `InvalidOperation` rather than returning
    #   False. A NaN cap booted cleanly and 500'd every priced Dry Run.
    dry_run_budget_cap_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = Field(
        default=None, alias="AGENTIVE_DRY_RUN_BUDGET_CAP_USD"
    )

    @field_validator("dry_run_budget_cap_usd", mode="before")
    @classmethod
    def _blank_budget_cap_to_none(cls, value: object) -> object:
        """An empty/whitespace env var means "unset", not "invalid decimal"."""
        return None if isinstance(value, str) and not value.strip() else value

    # ─── Workflow Engine — Accusé de réception d'un run (Story 5.1) ───
    # L'accusé « Compris. Je mobilise [agents]. ETA ~[X] min. » est calculé
    # SANS aucun appel LLM, sur le chemin synchrone de
    # `POST /workflows/{id}/runs` : ces deux réglages bornent ce calcul.
    #
    # Combien de runs passés on relit pour estimer la durée d'un node.
    # `le=100` comme `dry_run_history_limit`, et pour la même raison : la
    # lecture est synchrone dans le lancement d'un run, et l'AC2 promet une
    # première frame SSE en moins de 2 s.
    acknowledgement_history_limit: int = Field(
        default=20, ge=1, le=100, alias="AGENTIVE_ACK_HISTORY_LIMIT"
    )
    # Le temps MAXIMUM que cette lecture a le droit de prendre. La borne de
    # volume ci-dessus ne borne pas la latence : une base contendue, un
    # autovacuum sur `workflow_runs`, un failover en cours, et la lecture
    # traîne sans qu'aucun `except` ne s'en aperçoive — les exceptions
    # attrapent les erreurs, jamais la lenteur. Dépassé, l'estimation retombe
    # en `heuristic` et le run démarre : c'est une RÉCONCILIATION, elle n'a
    # pas le droit de tenir le lancement en otage.
    #
    # 0.5 s : un quart du budget de 2 s de l'AC2, qui laisse la marge à la
    # Mise en Place (elle, fait de l'I/O réseau) et à l'INSERT du run.
    acknowledgement_history_timeout_s: float = Field(
        default=0.5, gt=0, le=5.0, alias="AGENTIVE_ACK_HISTORY_TIMEOUT_S"
    )
    # Durée supposée d'un node quand l'historique ne dit rien — un workflow
    # jamais exécuté, ou un node ajouté depuis. L'estimation est alors
    # étiquetée `eta_source="heuristic"` : le Dry Run a dû apprendre en revue
    # (`no_execution_history`, `node_estimate_from_fallback`) qu'un chiffre
    # estimé qui se présente comme mesuré est pire que pas de chiffre.
    #
    # 60 s : l'ordre de grandeur d'un node LLM avec ses retries, sans outils.
    # `le=3600` borne l'absurde — au-delà, l'ETA d'un DAG de taille normale
    # se compte en heures et l'accusé cesse d'informer.
    acknowledgement_default_node_duration_s: float = Field(
        default=60.0,
        gt=0.0,
        le=3600.0,
        alias="AGENTIVE_ACK_DEFAULT_NODE_DURATION_S",
    )

    # ─── Workflow Engine — Mise en Place automatique (Story 4.5) ───
    # Per-server MCP ping timeout for the `mcp_tools_reachable` check —
    # deliberately SHORTER than `infra/mcp/client.py`'s
    # `DEFAULT_DISCOVERY_TIMEOUT_S=10.0` (used at server REGISTRATION time,
    # a context where the user is watching one server come up). Here it
    # gates a synchronous step of `POST /workflows/{id}/runs`, potentially
    # pinging several servers in parallel — one bad server must not freeze
    # every workflow launch for 10s+.
    #
    # No second budget-cap field here (Dev Notes § Budget) —
    # `dry_run_budget_cap_usd` above is reused as-is: one global threshold,
    # never a second source of truth for the same number (D84 lesson).
    # `le=30.0`: an upper bound is part of the point. Without one, a
    # mistyped `...TIMEOUT_S=600` freezes every workflow launch for ten
    # minutes — precisely the failure the paragraph above claims to prevent
    # (review P6).
    mise_en_place_tool_ping_timeout_s: float = Field(
        default=3.0,
        gt=0.0,
        le=30.0,
        allow_inf_nan=False,
        alias="AGENTIVE_MISE_EN_PLACE_TOOL_PING_TIMEOUT_S",
    )
    # Wall-clock ceiling for ONE Mise en Place check (review P6). The ping
    # timeout above only bounds `discover_tools`; the namespace lookups and
    # the Dry Run reused by `budget_available` had no bound at all, so a slow
    # database could hang `POST /runs` indefinitely. Applied per check rather
    # than to the whole hook so that one stuck check still yields a report
    # carrying the other three real outcomes.
    mise_en_place_check_timeout_s: float = Field(
        default=15.0,
        gt=0.0,
        le=120.0,
        allow_inf_nan=False,
        alias="AGENTIVE_MISE_EN_PLACE_CHECK_TIMEOUT_S",
    )

    # ─── Boucle d'outils — plafonds (Story 5.0, AC4) ───
    # Une boucle d'outils est non bornée PAR NATURE : elle s'arrête quand le
    # modèle décide de ne plus appeler d'outil, ce qui n'est pas une garantie
    # mais un pari. Chaque tour est un appel LLM facturé et au moins un appel
    # d'outil. Ces plafonds — trois compteurs ici, plus le plafond d'horloge
    # murale plus bas — sont l'unique chose qui borne le coût
    # d'un nœud, et ils sont déployables : un opérateur qui voit une facture
    # dériver doit pouvoir les baisser sans livrer de code.
    #
    # Les bornes `le` sont chargées, comme sur les timeouts de Mise en Place
    # (revue P6) : sans elles, un `...MAX_ITERATIONS=1000` mal tapé rend le
    # plafond inopérant tout en donnant l'illusion qu'il existe.
    tool_loop_max_iterations: int = Field(
        default=8,
        ge=1,
        le=50,
        alias="AGENTIVE_TOOL_LOOP_MAX_ITERATIONS",
    )
    # Second plafond, et pas un doublon du premier : un seul tour peut porter
    # plusieurs `tool_calls` (les deux providers le permettent). Borner les
    # tours sans borner les appels laisserait 8 tours x N outils en parallèle.
    tool_loop_max_tool_calls: int = Field(
        default=24,
        ge=1,
        le=200,
        alias="AGENTIVE_TOOL_LOOP_MAX_TOOL_CALLS",
    )
    # Timeout d'UN appel d'outil. Distinct de `NODE_TIMEOUT_S` (qui borne un
    # appel LLM) et du timeout de découverte MCP (10s, à l'enregistrement) :
    # un outil qui travaille — un `grep` sur un monorepo — a légitimement
    # besoin de plus qu'un ping et de moins qu'un nœud entier.
    # ⚠️ Ce plafond est PAR APPEL, et il ne borne donc RIEN tout seul : le
    # produit `max_tool_calls x tool_call_timeout_s` vaut 12 min aux valeurs
    # par défaut. Ce qui borne réellement un nœud est
    # `tool_loop_max_wall_clock_s` ci-dessous (180 s), qui expire bien avant
    # ce produit — cet arrêt-là est donc le cas NORMAL dès qu'un outil est
    # lent, pas une exception. Un `tool_call_timeout_s` proche du plafond
    # d'horloge murale signifie qu'un seul appel lent consomme tout le nœud.
    tool_call_timeout_s: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        allow_inf_nan=False,
        alias="AGENTIVE_TOOL_CALL_TIMEOUT_S",
    )

    # Plafond d'horloge murale pour UNE exécution de nœud avec outils, et le
    # seul des quatre qui soit une borne PLATE plutôt qu'un facteur.
    #
    # Pourquoi il existe. Les trois plafonds ci-dessus bornent un PRODUIT :
    # `max_iterations x NODE_TIMEOUT_S x chaîne` d'appels LLM, plus
    # `max_tool_calls x tool_call_timeout_s` d'appels d'outils. Aux valeurs
    # par défaut ce produit vaut ~20 min par tentative, soit ~4,8 h une fois
    # passé dans `recovery.derive_stale_threshold_s` — et ce module dit
    # explicitement qu'un seuil « de plus d'une heure défait le worker de
    # recovery » (cf `MAX_RUNTIME_RETRIES`, Story 4.6 T8.5). Sans cette
    # borne plate, la Story 5.0 rendrait donc la détection de run planté
    # inutilisable, silencieusement : la dérivation continuerait de calculer
    # un nombre juste pour une formule devenue fausse.
    #
    # Pourquoi 180 s, et pourquoi ce nombre n'est pas libre. Ce plafond est un
    # CADRAN sur la latence de détection de panne, et le taux de change est
    # connu : `derive_stale_threshold_s` le multiplie par `(1 + retries)` puis
    # par la marge x2,5, donc **une seconde de budget d'outils en plus coûte
    # dix secondes de fenêtre de détection**. Deux bornes le coincent :
    #   - PLANCHER 120 s = `NODE_TIMEOUT_S` x la chaîne de providers, ce qu'un
    #     nœud SANS aucun outil peut légitimement prendre aujourd'hui en
    #     basculant d'Anthropic vers OpenAI. En dessous, on tuerait des nœuds
    #     sains : `derive_stale_threshold_s` REFUSE la valeur plutôt que de la
    #     subir. À 120 s pile, la fenêtre vaut 1392,5 s : PLUS COURTE qu'avant
    #     cette story (1617,5 s), parce que cette même story a cessé de facturer
    #     huit escalades de routage là où il n'en a jamais lieu qu'une. Le
    #     budget d'outils y est nul.
    #   - PLAFOND 200 s, et `le=200.0` le fait respecter. Ce n'est pas une
    #     précaution : c'est le calcul. À la pire combinaison LÉGALE des
    #     autres réglages (`retry_base=60`, `retry_max=300`, `escalation=60`,
    #     `handoff=45`), la fenêtre vaut `(W x 4 + 60x2 + 420 + 45x2) x 2,5`,
    #     qui franchit l'heure dès `W > 202,5`. Un `le` plus large rendrait
    #     donc la borne « moins d'une heure » fausse par un seul réglage mal
    #     tapé — exactement ce que `le=60.0` empêche sur l'escalade, et ce que
    #     `le=30.0` empêche sur le ping de Mise en Place.
    #     La contrepartie est assumée : la plage utile est 120-200 s. Qui veut
    #     plus de budget d'outils doit d'abord baisser autre chose.
    # À 180 s : 60 s de budget d'outils par tentative, et une fenêtre de
    # `(180 x 4 + 15x2 + 7 + 40) x 2,5 = 1992,5 s` (~33 min) contre ~27 min
    # avant cette story. C'est le prix payé, il est explicite, et il se règle.
    # 60 s suffisent largement aux outils en LECTURE SEULE qui sont les seuls
    # assignables en Sprint 2 (cf `docs/runbooks/rejeu-et-outils.md`).
    # Revue P14 — `ge=120.0` POSÉ ICI. Trois endroits (ce commentaire,
    # `recovery.py` et le runbook) affirmaient que la valeur était « REFUSÉE
    # au démarrage » ; le champ ne portait que `gt=0.0`, et le plancher
    # n'existait que dans `derive_stale_threshold_s`. Un
    # `AGENTIVE_TOOL_LOOP_MAX_WALL_CLOCK_S=60` passait donc la validation,
    # le process démarrait, répondait aux health checks — puis chaque run et
    # chaque flux SSE levaient un `ValueError` nu depuis une fonction de
    # DÉRIVATION, très loin de la faute. Le refus au démarrage est désormais
    # réel : pydantic rend une erreur de configuration nommée.
    #
    # Le garde de `derive_stale_threshold_s` reste, et c'est voulu : il est
    # l'autorité (il connaît `NODE_TIMEOUT_S` et la longueur de chaîne, que
    # `shared/config.py` ne peut pas importer sans inverser les couches), et
    # cette borne-ci en est le miroir avancé. Si les deux divergent un jour,
    # c'est la dérivation qui a raison.
    tool_loop_max_wall_clock_s: float = Field(
        default=180.0,
        ge=120.0,
        le=200.0,
        allow_inf_nan=False,
        alias="AGENTIVE_TOOL_LOOP_MAX_WALL_CLOCK_S",
    )

    @model_validator(mode="after")
    def _a_single_tool_call_cannot_outlive_the_whole_node(self) -> Settings:
        """Revue P22 — `tool_call_timeout_s` (`le=300`) pouvait légalement
        dépasser `tool_loop_max_wall_clock_s` (`le=200`).

        La conséquence était déjà écrite en prose au-dessus du champ — « un
        seul appel lent consomme tout le nœud » — et gardée nulle part : la
        boucle mourait alors systématiquement sur son plafond d'horloge, en
        ayant exécuté un seul outil, et le réglage `tool_call_timeout_s`
        n'avait plus aucun effet observable. Une combinaison qui ne peut rien
        vouloir dire est refusée, pas subie.
        """
        if self.tool_call_timeout_s > self.tool_loop_max_wall_clock_s:
            raise ValueError(
                f"AGENTIVE_TOOL_CALL_TIMEOUT_S ({self.tool_call_timeout_s}s) exceeds "
                f"AGENTIVE_TOOL_LOOP_MAX_WALL_CLOCK_S ({self.tool_loop_max_wall_clock_s}s): "
                "a single tool call would always consume the entire node and the loop "
                "would always die on its wall-clock ceiling"
            )
        return self

    # ─── Pôle Dev — serveur MCP de lecture de code (Story 5.2, défer D62) ───
    #
    # **C'est une règle de sécurité, et c'est pour ça qu'elle est ici.** D62
    # (ouvert par la Story 2.6) décrit littéralement une colonne
    # `tools.sandbox_profile JSONB`. Une allowlist de chemins qui décide de ce
    # qu'un agent LLM peut lire doit être lisible dans un diff git et revue
    # comme du code, pas modifiable par un `UPDATE`. La divergence assumée
    # vis-à-vis de la forme littérale de D62 est écrite dans
    # `docs/decisions/dev-pole-code-search-server.md`.
    #
    # Ces racines gouvernent DEUX choses, et les deux sont nécessaires :
    #   - les `--root` passés au serveur `code_search` (sa propre allowlist,
    #     seule frontière qui existe sous le repli `setrlimit`, lequel n'isole
    #     PAS le filesystem) ;
    #   - les `--ro-bind` du `SandboxProfile` dérivé sous bwrap.
    # Liste séparée par des virgules — lisible dans un `.env`, contrairement
    # au JSON qu'un `list[str]` exigerait de pydantic-settings.
    dev_code_roots_raw: str = Field(
        default="/app",
        alias="AGENTIVE_DEV_CODE_ROOTS",
    )
    # Les trois bornes de sortie du serveur, appliquées À LA SOURCE et non
    # après coup : `MAX_TOOL_RESULT_CHARS` (infra/mcp/tool_executor.py) tronque
    # déjà ce qui part dans le prompt, mais après avoir tout lu — un
    # `search_content` sur un monorepo paierait la lecture entière pour rendre
    # 8 000 caractères.
    #
    # Taille lue par appel de `read_file`. Le DÉFAUT est volontairement sous le
    # plafond de 8 000 caractères de l'enveloppe d'outil (`MAX_TOOL_RESULT_CHARS`,
    # infra/mcp/tool_executor.py) : `read_file` rend `next_offset` pour lire la
    # suite, donc pagineer coûte moins cher que tronquer deux fois.
    #
    # ⚠️ Le PLAFOND, lui, laisse monter bien au-delà, et la revue 5.2 a corrigé
    # le commentaire qui prétendait l'inverse. Au-delà d'environ 7 000 octets de
    # contenu, la réponse JSON dépasse l'enveloppe et `MAX_TOOL_RESULT_CHARS` la
    # coupe **au milieu du document** : le modèle reçoit alors du JSON non
    # parsable, pas un contenu tronqué proprement. C'est le prix d'un réglage
    # élevé, et il est ici écrit plutôt que découvert.
    dev_code_max_read_bytes: int = Field(
        default=6_000,
        ge=256,
        le=200_000,
        alias="AGENTIVE_DEV_CODE_MAX_READ_BYTES",
    )
    # Nombre de résultats rendus par `list_directory`, `find_files` et
    # `search_content`. Borné à 1 000 : au-delà, la borne n'en est plus une et
    # la troncature aval reprendrait la main sans rien économiser en lecture.
    dev_code_max_results: int = Field(
        default=100,
        ge=1,
        le=1_000,
        alias="AGENTIVE_DEV_CODE_MAX_RESULTS",
    )
    # Profondeur de parcours sous le répertoire de départ. Ce qu'elle borne
    # n'est pas la sortie mais le COÛT : un lien ou une arborescence générée
    # peuvent faire tourner un parcours bien plus longtemps que ce que la
    # réponse laisse voir.
    dev_code_max_depth: int = Field(
        default=12,
        ge=1,
        le=40,
        alias="AGENTIVE_DEV_CODE_MAX_DEPTH",
    )

    @property
    def dev_code_roots(self) -> tuple[str, ...]:
        """Les racines autorisées, normalisées et dédupliquées.

        Rendue en propriété plutôt qu'en champ `list[str]` : pydantic-settings
        décode un champ complexe en JSON AVANT toute validation, donc un
        `AGENTIVE_DEV_CODE_ROOTS=/app,/srv` aurait fait échouer le démarrage
        sur une `SettingsError` de parsing au lieu d'être lu.

        « Normalisées » est devenu vrai avec la revue de la Story 5.2 : la
        version d'origine faisait un `strip()` et dédoublonnait sur la CHAÎNE
        EXACTE. `/app,/app/` produisait donc deux entrées, et
        `code_search_profile` en dérivait `--ro-bind /app /app --ro-bind
        /app/ /app/` — un ro-bind en double, que bwrap refuse. Le test qui
        gardait cette propriété n'exerçait que le doublon exact, seul cas que
        la déduplication attrapait.
        """
        seen: dict[str, None] = {}
        for chunk in self.dev_code_roots_raw.split(","):
            root = chunk.strip()
            if root:
                seen.setdefault(_normalise_root(root), None)
        return tuple(seen)

    @field_validator("dev_code_roots_raw")
    @classmethod
    def _dev_code_roots_are_an_allowlist(cls, value: str) -> str:
        """Refuse au DÉMARRAGE ce qui ne serait pas une allowlist.

        Une racine vide, relative, ou égale à `/` annule le contrôle tout en
        ayant l'air d'en être un. Le serveur refuse déjà ces trois cas, mais il
        le fait au spawn — c'est-à-dire à la première Mise en Place, très loin
        du réglage fautif. Ici l'erreur nomme le réglage.
        """
        roots = [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        if not roots:
            raise ValueError(
                "AGENTIVE_DEV_CODE_ROOTS est vide — le serveur de lecture de code "
                "n'aurait aucune racine autorisée et ne pourrait pas démarrer."
            )
        for root in roots:
            if not root.startswith("/"):
                raise ValueError(
                    f"AGENTIVE_DEV_CODE_ROOTS contient une racine relative ({root!r}) : "
                    "le répertoire courant du sous-processus sandboxé n'est pas une "
                    "notion contrôlée. Utiliser un chemin absolu."
                )
            # Revue 5.2 — `..` passait ce validateur : `'/app/..'.rstrip('/')`
            # vaut `'/app/..'`, donc ni vide ni `/`. Or il RÉSOUT vers `/`, et
            # `code_search_profile` ro-bindait la chaîne brute : le sandbox
            # montait tout le filesystem. Refusé à la source, parce que la
            # valeur lue dans un diff git doit être celle qui s'applique.
            if ".." in PurePosixPath(root).parts:
                raise ValueError(
                    f"AGENTIVE_DEV_CODE_ROOTS contient un `..` ({root!r}) : la racine "
                    "réellement appliquée ne serait pas celle qu'on lit ici "
                    f"({_normalise_root(root)!r}). Écrire le chemin résolu."
                )
            if _normalise_root(root) == "/":
                raise ValueError(
                    "AGENTIVE_DEV_CODE_ROOTS contient `/` : une allowlist qui contient "
                    "la racine du filesystem n'est pas une allowlist."
                )
        return value

    # ─── Workflow Engine — node retry backoff (Story 4.6, défer D13) ───
    # Feed `domain/error_policy.backoff_delay_s`, which spaces successive
    # re-runs of a node's FULL provider chain (`error_policy.on_timeout =
    # retry_with_backoff`). Not a retry policy in themselves: how MANY
    # retries is the template's call (`error_policy.max_retries`), how long
    # to wait between them is the deployment's.
    #
    # `le` on both is load-bearing, but NOT via the mechanism this comment
    # used to name. It said "the ceiling keeps the derivation true", when
    # the derivation read these fields' DEFAULTS and never these fields —
    # a bound on an input a formula ignores cannot keep that formula true.
    # What actually held was the x2.5 margin: at 60.0/300.0 the worst case
    # is 1020s against a 1517.5s window, so it fits, with the margin down
    # from x2.5 to x1.49. These two ceilings were never chosen against the
    # formula; the fit was luck, and the comment described it as design.
    #
    # `recovery.derive_stale_threshold_s` now reads the configured values
    # (review lot 7, P-P), so the derivation is true at every legal value
    # and no longer leans on that coincidence. What the ceilings bound is
    # the THRESHOLD'S OWN GROWTH: they are inputs to it, and a 3000s delay
    # would push detection of a genuinely crashed run into the hours,
    # defeating the recovery worker — the same argument that caps what the
    # runtime executes at `MAX_RUNTIME_RETRIES = 3` rather than the
    # schema's 10.
    workflow_retry_base_delay_s: float = Field(
        default=1.0,
        gt=0.0,
        le=60.0,
        allow_inf_nan=False,
        alias="AGENTIVE_WORKFLOW_RETRY_BASE_DELAY_S",
    )
    workflow_retry_max_delay_s: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        allow_inf_nan=False,
        alias="AGENTIVE_WORKFLOW_RETRY_MAX_DELAY_S",
    )

    # ─── Story 4.7 — handoff summaries (FR53) ───
    # Same posture as `routing_escalation_model`: a fixed, lightweight,
    # PROCESS-WIDE model for an auxiliary engine call, never a per-agent
    # `provider_chain`/`error_policy` (Dev Notes § comment already written in
    # `hybrid_router.py` for the routing escalation call — a summary is a
    # function of the engine, not the agent template author's behaviour).
    workflow_handoff_summary_model: str = Field(
        default="claude-haiku-4-5",
        min_length=1,
        alias="AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_MODEL",
    )
    # A résumé is 4 short lists of strings — far below a content node's
    # `DEFAULT_MAX_TOKENS = 4096` (`agent_node.py`). `le` mirrors
    # `routing_escalation_max_tokens`: an auxiliary engine call has no
    # business outgrowing the node it serves.
    workflow_handoff_summary_max_tokens: int = Field(
        default=512,
        ge=1,
        le=4096,
        alias="AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_MAX_TOKENS",
    )
    # More generous than `routing_escalation_timeout_s` (15s): the input
    # being condensed here is a full content node's output, potentially much
    # larger than a routing classification's input.
    #
    # Review of 2026-09-12 (I-02) — `le=60.0` was MISSING, and this knob is
    # the twin of `routing_escalation_timeout_s` above, whose own ceiling
    # exists because `recovery.derive_stale_threshold_s` multiplies it (here,
    # by `_MAX_PROVIDER_CHAIN_LEN`). Unbounded, it moved two things at once,
    # both by one env var: the staleness window (past which a crashed run
    # sits unexamined — at 600 s the window passes three hours), and the
    # worst-case latency of a `pause`/`cancel`, which is observed only at the
    # superstep boundary and now waits for this call before reaching it.
    #
    # 45.0 rather than `routing_escalation_timeout_s`'s 60.0, and the number
    # is DERIVED, not picked: `derive_stale_threshold_s` adds this term as
    # `h * _MAX_PROVIDER_CHAIN_LEN * _SAFETY_MARGIN` = `5h` on top of
    # 1517.5 s at otherwise-default settings, and `DEFAULT_STALE_THRESHOLD_S`
    # commits in writing to staying under 30 min. `1517.5 + 5h < 1800` gives
    # `h < 56.5`; 45.0 lands at 1742.5 s (29.0 min) with margin to spare,
    # while still granting more than twice the 20.0 default. At 60.0 the
    # window would be 1817.5 s — 30.3 min — quietly breaking that promise
    # through a knob nobody would think to check.
    workflow_handoff_summary_timeout_s: float = Field(
        default=20.0,
        gt=0.0,
        le=45.0,
        allow_inf_nan=False,
        alias="AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_TIMEOUT_S",
    )
    # Review of 2026-09-12 (I-04) — the deployment-level kill switch Story
    # 4.7 shipped without.
    #
    # AC2 makes summarization the DEFAULT, which silently changes the prompt
    # of every template already in production: one written and validated
    # against an upstream node's raw output (reading, say,
    # `upstream_outputs["a"]["invoice_id"]`) stops finding that field, with
    # no change to its own config and no template versioning to roll back to.
    # The per-template opt-out exists, but it is per-template: recovering
    # from a bad rollout meant editing every consumer under incident.
    #
    # `False` makes `_execute` pass `handoff_settings=None`, which is the
    # path `execute_agent_node`/`build_state_graph` already default to and
    # already test — not a new branch, the pre-4.7 behaviour byte for byte.
    # Default `True`: the AC's default stands, and this only makes it
    # reversible without a deploy of template edits.
    workflow_handoff_summary_enabled: bool = Field(
        default=True,
        alias="AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_ENABLED",
    )

    # ─── Story 4.14 — bounding the `FOR SHARE` wait on workflow creation ───
    # Review of Story 4.8 (D3) — `create_workflow`'s single transaction
    # (Story 4.8 AC2) holds `FOR SHARE` on every referenced `agent_templates`
    # row for the duration of the transaction, including whatever it spends
    # blocked on `uq_workflow_request_fingerprint` when a concurrent replay
    # loses the race (Story 4.8 AC1). Nothing bounded that wait: a stalled
    # winner left the loser parked indefinitely, and a concurrent
    # `PUT /agents/templates/{id}` queued behind it for just as long.
    #
    # Scoped to THIS transaction alone via `SET LOCAL lock_timeout`
    # (`WorkflowRepo.with_tenant`'s `lock_timeout_ms=` — mirror the existing
    # `SET LOCAL app.tenant_id` pattern), not a role-wide `statement_timeout`:
    # a global timeout is a behaviour change for every query the engine
    # issues and is not a decision this single surface gets to make.
    #
    # 5.0s default: generous against the transaction's own worst case (one
    # batch SELECT, pure-CPU validation, one INSERT — Story 4.8 Dev Notes),
    # so ordinary contention never trips it, while still turning an
    # indefinite wait into a typed, bounded failure.
    # Floor is 1ms, not "anything above zero": the value is converted with
    # `int(s * 1000)` at the call site, so `gt=0.0` used to admit e.g. 0.0004,
    # which truncates to `0` — and Postgres reads `lock_timeout = 0` as
    # DISABLED. A setting that reads as "time out almost instantly" silently
    # restored the unbounded wait this whole mechanism exists to remove
    # (review 4.14, finding 4). `ge=0.001` makes the smallest admissible
    # value the smallest one Postgres can actually express.
    workflow_create_lock_timeout_s: float = Field(
        default=5.0,
        ge=0.001,
        le=30.0,
        allow_inf_nan=False,
        alias="AGENTIVE_WORKFLOW_CREATE_LOCK_TIMEOUT_S",
    )

    # ─── Story 4.9 AC2 — load bounds on run creation/resume ───
    # No cap existed on how many runs of one workflow can be `running` at
    # once: `POST /runs` in a burst (or a client retry loop) could exhaust
    # the LLM provider pool or process memory with no typed refusal — only
    # eventual, opaque provider/DB errors. Per-workflow rather than global:
    # one noisy workflow must not starve every other workflow's admission
    # budget, and a global counter would need its own re-justification for
    # the same reason a per-template `tool_ids` cap beat a global one.
    # Explicitly NOT a cost/rate-limiting policy (Story 9.4/9.5 own that) —
    # this is a mechanical ceiling on concurrent DB rows and in-flight
    # background tasks, nothing more.
    workflow_max_concurrent_running_runs: int = Field(
        default=20,
        ge=1,
        le=1000,
        alias="AGENTIVE_WORKFLOW_MAX_CONCURRENT_RUNS",
    )

    # ─── Story 4.10 AC1 — checkpoint blob retention ───
    # `checkpoints`/`checkpoint_writes`/`checkpoint_blobs` (migration
    # `20260910_000001`) are never purged by any code in this repo — a
    # terminal run's full node-output history (up to 500 chars of it
    # previewed in the applicative `checkpoint` JSONB, the REST persisted
    # here without a size cap) accumulates forever. 90 days mirrors the
    # `audit_events` retention precedent (`architecture.md` § *Authentication
    # & Security*) rather than inventing a second, unrelated number.
    workflow_checkpoint_retention_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        alias="AGENTIVE_WORKFLOW_CHECKPOINT_RETENTION_DAYS",
    )
    # How often `CheckpointRetentionWorker` sweeps for purgeable runs.
    # Daily, like `MemoryArchivalWorker` (Story 3.3) — this is a housekeeping
    # job, not a latency-sensitive one, and there is no operational reason
    # to poll it more often than once a day.
    workflow_checkpoint_retention_interval_s: float = Field(
        default=86_400.0,
        gt=0.0,
        le=604_800.0,
        allow_inf_nan=False,
        alias="AGENTIVE_WORKFLOW_CHECKPOINT_RETENTION_INTERVAL_S",
    )

    # ─── Story 4.10 AC2 — alerting on immortal `paused` runs ───
    # A `paused` run is deliberately excluded from `claim_stale_running`
    # (nothing should auto-resume a run a human explicitly suspended) and
    # `ended_at` stays NULL, so nothing ever expires it. Decision taken
    # (T2.1): ALERT, not automatic TTL/cancellation — silently cancelling a
    # run an operator paused on purpose (e.g. "fix a bad namespace, resume
    # next week") is a hard-to-reverse, surprising action for this codebase
    # to take unprompted; a log-based alert an operator can act on is not.
    # 7 days default: long enough that a normal maintenance pause never
    # triggers it, short enough that a genuinely forgotten run is caught
    # well before it becomes a "why does this workflow have 40 stuck runs"
    # incident.
    workflow_paused_run_alert_after_days: int = Field(
        default=7,
        ge=1,
        le=3650,
        alias="AGENTIVE_WORKFLOW_PAUSED_RUN_ALERT_AFTER_DAYS",
    )

    # ─── Story 4.10 AC4 — bounding the `routing-stats` aggregation ───
    # `GET /workflows/{id}/routing-stats` aggregated `metrics` JSONB over
    # EVERY run of a workflow, with no window: a workflow with 200k runs paid
    # a cost proportional to a number entirely within the CALLER's control
    # (how many times they hit `POST /runs`), not the operator's. 90 days —
    # same retention precedent as the checkpoint window above — turns the
    # metric from an all-time proportion into a trailing-window one; the
    # response says which (`window_days`), so a consumer never has to guess
    # what changed.
    workflow_routing_stats_window_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        alias="AGENTIVE_WORKFLOW_ROUTING_STATS_WINDOW_DAYS",
    )

    # ─── CORS ───
    # JSON-parsed from env (e.g. `AGENTIVE_CORS_ALLOW_ORIGINS='["https://app.example.com"]'`).
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "https://localhost:8443"],
        alias="AGENTIVE_CORS_ALLOW_ORIGINS",
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Computed DSNs — URL-encoded to support passwords with `@`, `/`, `:`, `%`.
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> PostgresDsn:
        """Runtime DSN for agentive_app (RLS-scoped)."""
        encoded = quote_plus(self.postgres_app_password.get_secret_value())
        return PostgresDsn(
            f"postgresql+psycopg://agentive_app:{encoded}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_owner(self) -> PostgresDsn:
        """DSN for Alembic migrations (agentive_owner role)."""
        encoded = quote_plus(self.postgres_owner_password.get_secret_value())
        return PostgresDsn(
            f"postgresql+psycopg://agentive_owner:{encoded}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def psycopg_dsn(self) -> str:
        """:attr:`database_url` as a plain ``postgresql://`` DSN.

        For the consumers that speak raw psycopg rather than SQLAlchemy —
        ``LISTEN/NOTIFY`` in the outbox publisher, and LangGraph's
        ``PostgresSaver``/``AsyncPostgresSaver`` (Story 4.2). See
        :func:`to_psycopg_dsn`.
        """
        return to_psycopg_dsn(str(self.database_url))

    @property
    def psycopg_dsn_owner(self) -> str:
        """:attr:`database_url_owner` as a plain ``postgresql://`` DSN —
        the ``agentive_owner`` counterpart of :attr:`psycopg_dsn`, used by
        migrations that must issue DDL."""
        return to_psycopg_dsn(str(self.database_url_owner))

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Validators
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @model_validator(mode="after")
    def _reject_dev_defaults_in_production(self) -> Settings:
        """Fail-fast on `change_me*` placeholder in production; warn in dev."""
        violations: list[str] = []
        secret_fields = {
            "AGENTIVE_API_TOKEN": self.agentive_api_token.get_secret_value(),
            "AGENTIVE_ENCRYPTION_KEY": self.agentive_encryption_key.get_secret_value(),
            "POSTGRES_APP_PASSWORD": self.postgres_app_password.get_secret_value(),
            "POSTGRES_OWNER_PASSWORD": self.postgres_owner_password.get_secret_value(),
        }
        for name, value in secret_fields.items():
            if any(sentinel in value for sentinel in _DEV_DEFAULT_SENTINELS):
                violations.append(name)

        if not violations:
            return self

        if self.environment == "production":
            raise ValueError(
                "Refusing to start in production with dev placeholder values for: "
                f"{', '.join(violations)}. Set the corresponding env vars to real secrets."
            )

        # Development / test : warn loudly but do not block.
        import sys

        print(
            "⚠️  agentive-backend config WARNING: dev placeholder values detected for "
            f"{', '.join(violations)}. "
            "The default AGENTIVE_API_TOKEN is PUBLIC (committed in .env.example) — "
            "anyone can authenticate. Override via environment variables for any "
            "non-throwaway deployment.",
            file=sys.stderr,
        )
        return self

    @model_validator(mode="after")
    def _reject_invalid_fernet_key_in_production(self) -> Settings:
        """Validate the Fernet encryption key format(s) in production.

        Validates ``AGENTIVE_ENCRYPTION_KEY`` always, and
        ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS`` too when set (Story 9.2 AC3
        rotation window) — a malformed previous key would silently make
        rotation's decrypt-fallback a no-op instead of failing fast.
        """
        if self.environment != "production":
            return self

        keys = {"AGENTIVE_ENCRYPTION_KEY": self.agentive_encryption_key}
        if self.agentive_encryption_key_previous is not None:
            keys["AGENTIVE_ENCRYPTION_KEY_PREVIOUS"] = self.agentive_encryption_key_previous

        for env_name, secret in keys.items():
            key = secret.get_secret_value()
            try:
                Fernet(key.encode() if isinstance(key, str) else key)
            except (ValueError, InvalidToken) as exc:
                raise ValueError(
                    f"{env_name} is not a valid Fernet key "
                    "(expected 32 url-safe base64 bytes). "
                    "Generate one with: `python -c 'from cryptography.fernet import Fernet;"
                    " print(Fernet.generate_key().decode())'`."
                ) from exc
        return self


# SQLAlchemy dialect markers that a raw psycopg / LangGraph consumer must not
# see. `postgresql://` is listed so an already-converted DSN round-trips.
_SQLALCHEMY_DIALECT_PREFIXES = (
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgresql://",
)


def to_psycopg_dsn(url: str) -> str:
    """Strip SQLAlchemy's dialect marker so raw psycopg / LangGraph can connect.

    Single home for a conversion that was open-coded in five places (app
    lifespan, outbox publisher, the T1.2 checkpointer migration, and two test
    fixtures). Each copy was a bare ``.replace(...)``, which is a SILENT no-op
    on any unexpected scheme: a mistyped or future DSN would sail through
    unconverted and only surface as a connection error somewhere far away.
    This raises instead.

    Raises:
        ValueError: ``url`` does not carry a recognised PostgreSQL scheme.
    """
    for prefix in _SQLALCHEMY_DIALECT_PREFIXES:
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    raise ValueError(
        f"not a recognised PostgreSQL DSN: {url.split('://', 1)[0]!r} "
        f"(expected one of {_SQLALCHEMY_DIALECT_PREFIXES})"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return singleton Settings instance (cached)."""
    return Settings()


settings = get_settings()
