"""Event type naming convention — ``module.entity.action``.

Examples
--------
Valid : ``system.app.started``, ``workflow_engine.workflow.started``, ``memory_manager.chunk.indexed``
Invalid : ``System.Started`` (uppercase), ``a.b.c.d`` (4 segments),
``workflow_engine.workflow`` (2 segments).

The module prefix IS the feature package name (``features/<module>/``) —
one name per bounded context everywhere (ADR ``docs/decisions/module-naming.md``).

Why three segments
------------------
Two segments would conflate domain and operation; four would force naming
contortions (``workflow_engine.workflow.run.started`` vs ``workflow_engine.workflow.started``). Three
matches the canonical CloudEvents ``source/type`` split.
"""

from __future__ import annotations

import re

from agentive_backend.shared.event_bus.exceptions import InvalidEventTypeError
from agentive_backend.shared.logging import get_logger

# Module prefixes whitelisted for warn-on-typo. Permissive (does not raise).
# Rule: prefix == feature package name under `features/` (+ "system" for
# app-lifecycle events). Keep in sync when adding a feature module.
KNOWN_MODULE_PREFIXES: frozenset[str] = frozenset(
    {
        "system",
        "agent_configurator",
        "agent_registry",
        "chat",
        "company_architect",
        "dashboard",
        "memory_manager",
        "playground",
        "reporting",
        "scheduler",
        "tool_hub",
        "topology",
        "trace",
        "workflow_engine",
    }
)

# Lowercase, three dot-separated segments. Each segment starts with a letter,
# may contain letters / digits / underscore. No leading underscore on segments.
# (The module segment allows underscores since the DDD rename — prefixes are
# package names like `agent_registry`, no longer `m2`.)
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

# Hard cap to prevent pathological event_type names from approaching the
# Postgres NOTIFY 8000-byte payload limit (P11). Our NOTIFY message format
# is ``<uuid>:<event_type>`` — UUID is 36 chars, separator 1, leaving plenty
# of headroom under 200.
_MAX_EVENT_TYPE_LEN = 200

_log = get_logger(__name__)


def validate_event_type(event_type: str) -> None:
    """Raise :class:`InvalidEventTypeError` if ``event_type`` is malformed.

    Emits a structlog warning via the project logger (P13) if the module
    prefix is unknown — typos are common when adding new feature modules and
    a hard failure would discourage experiments. The structlog pipeline's
    ``_add_correlation_id`` processor decorates the warning automatically.
    """
    if not isinstance(event_type, str) or not _EVENT_TYPE_RE.match(event_type):
        raise InvalidEventTypeError(
            f"event_type must match `module.entity.action` lowercase pattern, got {event_type!r}"
        )
    if len(event_type) > _MAX_EVENT_TYPE_LEN:
        raise InvalidEventTypeError(
            f"event_type exceeds {_MAX_EVENT_TYPE_LEN}-char cap "
            f"(NOTIFY 8000-byte payload safety, got {len(event_type)})"
        )
    prefix = event_type.split(".", 1)[0]
    if prefix not in KNOWN_MODULE_PREFIXES:
        _log.warning(
            "event_bus.unknown_module_prefix",
            prefix=prefix,
            event_type=event_type,
            note="Typo? Add to KNOWN_MODULE_PREFIXES if intentional.",
        )
