"""Execute a model-requested tool against MCP (Story 5.0 AC2/AC3).

The injected half of :mod:`agentive_backend.shared.llm.tool_loop`. The loop
lives in ``shared`` and cannot reach MCP (``.import-linter`` Contract 2:
``shared`` never imports ``infra``); this module is the ``infra`` side it
receives by injection.

**Why here and not in a feature.** Both ``playground`` and ``workflow_engine``
need it, and Contract 1 forbids them to import each other. Putting it in
either would force the other to duplicate it — which is exactly the debt this
codebase already carries on ``PlaygroundService``'s constants ("Keep in sync
manually", no parity test). ``infra`` is the lowest layer both can reach.

**It takes tools already resolved, and does no database access.** The caller
loads its assignments the way it already does, hands over the resolved set,
and this module stays a pure MCP adapter — testable without a repo, a session
or a migration.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

from agentive_backend.infra.mcp.client import (
    MCPExecutionTimeoutError,
    MCPToolError,
    call_tool,
)
from agentive_backend.infra.mcp.policy import apply_sandbox_policy
from agentive_backend.shared.config import settings
from agentive_backend.shared.llm.security import wrap_external_input
from agentive_backend.shared.llm.types import ToolCall, ToolDefinition
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

#: Cap on the tool result text handed back to the model. A tool is an
#: unbounded source of text (a `grep` over a monorepo, a CI log), and the
#: mechanism meant to let an agent read things must not become the most
#: direct way to blow up a prompt — the same lesson Story 4.7 P-9 learned on
#: handoff summaries, applied before it bites rather than after.
MAX_TOOL_RESULT_CHARS = 8_000


@dataclass(frozen=True)
class ResolvedTool:
    """An assigned tool, joined with what it takes to actually call it."""

    tool_id: UUID
    server_id: UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    transport: Literal["stdio", "sse"]
    connection_config: dict[str, Any]


@dataclass
class ToolInvocation:
    """What one execution did — the raw material for the Playground's
    ``tool_invocations`` (AC6) and for per-node metrics."""

    #: ``None`` UNIQUEMENT quand le modèle a réclamé un outil qui ne lui a
    #: pas été offert : il n'existe alors aucune ligne `tools` ni aucun
    #: serveur à nommer (revue P11). Tout appel résolu les porte.
    tool_id: UUID | None
    server_id: UUID | None
    tool_name: str
    arguments: dict[str, Any]
    result_summary: str
    duration_ms: int
    status: Literal["success", "error", "timeout"]


#: Le schéma minimal qu'un provider accepte pour un outil sans argument.
#: Anthropic REFUSE un `input_schema` vide (`input_schema.type: Field
#: required`, 400) et OpenAI attend également un objet typé.
_EMPTY_OBJECT_SCHEMA: Final[dict[str, Any]] = {"type": "object", "properties": {}}

#: Miroirs des bornes de `ToolDefinition`. Dupliqués ici À DESSEIN :
#: `shared/llm/types.py` les fait respecter en LEVANT, ce qui est juste
#: pour un contrat, et cette projection doit au contraire ABSORBER ce
#: qu'un serveur MCP annonce sans faire tomber le run (revue P5).
_MAX_TOOL_NAME_CHARS: Final = 128
_MAX_TOOL_DESCRIPTION_CHARS: Final = 2_048


def to_tool_definitions(resolved: Mapping[str, ResolvedTool]) -> list[ToolDefinition]:
    """Project resolved tools into the provider-agnostic contract the loop
    offers to the model.

    ``input_schema`` vide est NORMALISÉ plutôt que transmis tel quel (revue
    P6). Ce n'est pas un cas de bord : `tools.input_schema` porte
    ``server_default='{}'::jsonb``, donc ``{}`` est la valeur PERSISTÉE PAR
    DÉFAUT pour tout outil dont le serveur MCP n'annonce pas de schéma. Le
    transmettre faisait échouer le run entier par un 400 provider — fatal,
    sans repli — pour un outil parfaitement légitime qui ne prend simplement
    aucun argument.
    """
    definitions: list[ToolDefinition] = []
    for t in resolved.values():
        if len(t.name) > _MAX_TOOL_NAME_CHARS:
            # Revue P5 — un nom ne se tronque PAS : c'est l'identifiant que le
            # modèle renverra, et le raccourcir ferait échouer la résolution
            # sur un nom qui n'existe pas. L'outil est donc écarté, bruyamment
            # — offrir un outil inappelable est pire que ne pas l'offrir.
            _log.warning(
                "mcp.tool_name_too_long",
                tool=t.name[:64],
                length=len(t.name),
                ceiling=_MAX_TOOL_NAME_CHARS,
            )
            continue
        description = t.description
        if len(description) > _MAX_TOOL_DESCRIPTION_CHARS:
            # Une description, SI : elle est indicative, et beaucoup de
            # serveurs MCP réels y inlinent des exemples d'usage. La tronquer
            # dégrade la qualité de l'appel ; laisser la `ValidationError`
            # remonter tuait le run entier (500 non audité) pour un outil
            # parfaitement fonctionnel.
            _log.warning(
                "mcp.tool_description_truncated",
                tool=t.name,
                length=len(description),
                ceiling=_MAX_TOOL_DESCRIPTION_CHARS,
            )
            description = description[: _MAX_TOOL_DESCRIPTION_CHARS - 1] + "…"
        definitions.append(
            ToolDefinition(
                name=t.name,
                description=description,
                input_schema=t.input_schema or dict(_EMPTY_OBJECT_SCHEMA),
            )
        )
    return definitions


@dataclass
class McpToolExecutor:
    """Callable matching ``shared.llm.tool_loop.ToolExecutor``.

    **Never raises for an ordinary tool failure.** A timeout, an MCP
    ``isError`` result or an unknown tool name is information the model can
    act on — it can try another tool, or say it could not find out. Raising
    would kill a run and throw away everything already billed, which is the
    opposite of what the surrounding error handling is for. Only a
    programming error escapes.
    """

    resolved: Mapping[str, ResolvedTool]
    invocations: list[ToolInvocation] = field(default_factory=list)

    async def __call__(self, call: ToolCall) -> str:
        started = time.perf_counter()
        tool = self.resolved.get(call.name)

        if tool is None:
            # The model asked for something it was not offered. Told plainly
            # rather than swallowed: an agent that hallucinates a tool name
            # can correct itself if it is told, and can do nothing at all if
            # the result is an empty string.
            _log.warning(
                "mcp.tool_executor_unknown_tool",
                requested=call.name,
                offered=sorted(self.resolved),
            )
            # Revue P11 — enregistré comme n'importe quel autre échec. Ce
            # retour anticipé n'alimentait rien, alors que `run_tool_loop`
            # incrémente `tool_calls_made` inconditionnellement : un modèle
            # qui hallucine huit fois le même nom produisait
            # `tool_calls: 8, tool_names: [], tool_failures: 0` — trois
            # champs qui se contredisent, et un node « réussi » dont
            # personne ne pouvait expliquer le prix. C'est exactement l'angle
            # mort que le commentaire de `tool_names` dit exister pour
            # éclairer.
            self.invocations.append(
                ToolInvocation(
                    tool_id=None,
                    server_id=None,
                    tool_name=call.name,
                    arguments=call.arguments,
                    result_summary="ERROR: unknown tool",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    status="error",
                )
            )
            return self._wrap(f"ERROR: no tool named {call.name!r} is available to you.")

        status: Literal["success", "error", "timeout"]
        # Story 5.2 T2.2 — le profil sandbox du SERVEUR, et la configuration
        # de connexion qu'il réécrit. Avant cette story aucun appelant ne
        # passait `profile=`, donc le défaut s'appliquait partout : il ne
        # ro-bind que `/usr /etc /lib /lib64 /bin /sbin`, et sous bwrap un
        # serveur de lecture de code n'aurait vu ni le code ni son propre
        # interpréteur.
        try:
            # Story 5.2 — DANS le `try`, et c'est une correction de la revue.
            # L'appel précédait le `try`, donc une exception de la politique
            # (réglage incohérent, interpréteur introuvable) échappait à
            # `__call__` et tuait la boucle d'outils — le contre-exemple exact
            # de la règle que ce bloc défend trente lignes plus bas : « le
            # modèle peut corriger ; il ne peut rien faire d'un run mort ».
            profile, connection_config = apply_sandbox_policy(
                transport=tool.transport,
                connection_config=tool.connection_config,
            )
            raw = await call_tool(
                transport=tool.transport,
                connection_config=connection_config,
                tool_name=tool.name,
                arguments=call.arguments,
                profile=profile,
                # AC4, troisième plafond. Passé EXPLICITEMENT plutôt que
                # laissé au défaut de `call_tool` : le défaut de ce module-là
                # est dimensionné pour un appel isolé (Dry Run, Playground
                # manuel), pas pour une boucle qui peut en enchaîner 24.
                timeout=settings.tool_call_timeout_s,
            )
            text = self._render(raw)
            # MCP reports a tool-level failure in the payload, not by raising.
            # Treating that as success would hand the model an error message
            # formatted as a result.
            status = "error" if raw.get("isError") else "success"
        except MCPExecutionTimeoutError as exc:
            status = "timeout"
            text = f"ERROR: tool {tool.name!r} timed out: {exc}"
            _log.warning("mcp.tool_executor_timeout", tool=tool.name)
        except MCPToolError as exc:
            # Story 5.2 T1.4 — un refus d'outil est une information que le
            # modèle peut EXPLOITER, et le `except Exception` ci-dessous la
            # réduisait au seul nom de classe : « ERROR: tool 'read_file'
            # failed: MCPToolError » ne dit pas au modèle de corriger son
            # chemin. `MCPToolError.detail` est le texte que le serveur MCP a
            # lui-même renvoyé — c'est de la sortie d'outil, exactement comme
            # un succès, donc pas plus risquée que lui : `_wrap` l'enveloppe
            # dans `<tool_output>` avant qu'elle n'atteigne le prompt.
            #
            # Distinct du `except Exception` qui suit, et cette distinction
            # est le point : une exception PYTHON peut porter un DSN, un
            # chemin interne ou un jeton (NFR9), et celle-là reste muette.
            status = "error"
            text = f"ERROR: tool {tool.name!r} refused the call: {exc.detail}"
            _log.warning("mcp.tool_executor_tool_error", tool=tool.name)
        except Exception as exc:
            status = "error"
            text = f"ERROR: tool {tool.name!r} failed: {type(exc).__name__}"
            # Logged with the traceback, but NOT surfaced to the model: an
            # exception message can carry a DSN, a path or a token, and this
            # string goes straight into a prompt (NFR9).
            _log.exception("mcp.tool_executor_failed", tool=tool.name)

        duration_ms = int((time.perf_counter() - started) * 1000)
        self.invocations.append(
            ToolInvocation(
                tool_id=tool.tool_id,
                server_id=tool.server_id,
                tool_name=tool.name,
                arguments=call.arguments,
                result_summary=text[:500],
                duration_ms=duration_ms,
                status=status,
            )
        )
        return self._wrap(text)

    @staticmethod
    def _render(raw: dict[str, Any]) -> str:
        """Flatten an MCP ``CallToolResult`` into text the model can read.

        Prefers the ``content`` blocks the protocol defines; falls back to
        JSON rather than to ``str(dict)``, which would hand the model Python
        repr syntax it has no reason to parse.
        """
        content = raw.get("content")
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if any(parts):
                return "\n".join(p for p in parts if p)
        try:
            return json.dumps(raw, ensure_ascii=False, default=str)
        except TypeError, ValueError:  # pragma: no cover — defensive
            return str(raw)

    @staticmethod
    def _escaped_len(text: str) -> int:
        """Length ``text`` will have once ``wrap_external_input`` escapes it.

        ``html.escape(quote=False)`` maps ``&`` to ``&amp;`` (5 chars) and
        ``<``/``>`` to ``&lt;``/``&gt;`` (4 chars). Everything else is
        length-preserving, so the cost is a per-character sum.
        """
        return sum(5 if ch == "&" else 4 if ch in "<>" else 1 for ch in text)

    @classmethod
    def _truncate_to_escaped_budget(cls, text: str, budget: int) -> tuple[str, bool]:
        """Largest prefix of ``text`` whose ESCAPED form fits in ``budget``.

        Cuts on the RAW string — never on the escaped one — so the result can
        still be handed to ``wrap_external_input`` for a single, correct
        escape pass. Slicing escaped text would risk severing an entity
        (``&am``) and would double-escape if re-wrapped.
        """
        if cls._escaped_len(text) <= budget:
            return text, False
        cost = 0
        for i, ch in enumerate(text):
            cost += 5 if ch == "&" else 4 if ch in "<>" else 1
            if cost > budget:
                return text[:i], True
        return text, False

    @classmethod
    def _wrap(cls, text: str) -> str:
        """Truncate, then wrap — and this is the ONLY exit of this class.

        ``wrap_external_input`` is applied HERE, one layer below the loop, so
        that no caller can forget it. That is not paranoia: this is the fourth
        prompt-building surface in the repo, and the omission on the third
        (``PlaygroundService``) survived two full epics before a review found
        it (Story 9.7). Placing the guard at the only exit makes a fifth
        omission structurally impossible rather than merely discouraged.

        The cap is applied to the ESCAPED length, not the raw one (review
        P9). Truncating the raw string first and escaping afterwards left the
        ceiling unenforced in exactly the cases it exists for: escaping
        expands ``&`` five-fold and ``<``/``>`` four-fold, so 8 000 raw chars
        of HTML, XML or a diff — a ``grep`` over a monorepo, this module's own
        stated use case — reached the prompt as up to 40 000. The previous
        docstring claimed this ordering made "the cap apply to what the model
        actually reads"; it did the opposite, and the test only probed ``"A"``,
        a character escaping leaves alone.
        """
        text, truncated = cls._truncate_to_escaped_budget(text, MAX_TOOL_RESULT_CHARS)
        if truncated:
            text += "\n[… tronqué]"
        return wrap_external_input(text, "tool_output")
