"""Event type naming convention — ``module.entity.action``.

Examples
--------
Valid : ``system.app.started``, ``m3.workflow.started``, ``m4.chunk.indexed``
Invalid : ``System.Started`` (uppercase), ``a.b.c.d`` (4 segments),
``m3.workflow`` (2 segments).

Why three segments
------------------
Two segments would conflate domain and operation; four would force naming
contortions (``m3.workflow.run.started`` vs ``m3.workflow.started``). Three
matches the canonical CloudEvents ``source/type`` split.
"""

from __future__ import annotations

import re

from agentive_backend.shared.event_bus.exceptions import InvalidEventTypeError
from agentive_backend.shared.logging import get_logger

# Module prefixes whitelisted for warn-on-typo. Permissive (does not raise).
KNOWN_MODULE_PREFIXES: frozenset[str] = frozenset(
    {
        "system",
        "m1",
        "m2",
        "m3",
        "m4",
        "m5",
        "m6",
        "m7",
        "m8",
        "m9",
        "m10",
        "m11",
        "m12",
    }
)

# Lowercase, three dot-separated segments. Each segment starts with a letter,
# may contain letters / digits / underscore. No leading underscore on segments.
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

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
