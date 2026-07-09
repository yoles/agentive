"""Cross-consistency tests for key-based secret redaction (audit A-02/6.4b).

The 2026-07 audit found that ``_redact_connection_config`` (exact-match,
6 keys) and ``_redact_arguments`` (substring patterns, 12) had DIVERGENT
coverage: a secret stored under ``bearer`` / ``credential`` / ``passphrase``
/ ``client_secret`` / ``access_key`` leaked in clear through
``GET /tools/servers/{id}`` while being masked in tool arguments. Each
function was only ever tested against its OWN key set, so the divergence
passed green.

This module is the table-driven guard the audit called for: for every
canonical secret key, BOTH redaction paths must mask. If someone forks the
predicate again, this test fails.
"""

from __future__ import annotations

import pytest

from agentive_backend.features.tool_hub.service import (
    _redact_arguments,
    _redact_connection_config,
)
from agentive_backend.shared.security import (
    REDACTED,
    is_secret_key,
    redact_recursive,
)

# Canonical secret keys — one representative per SECRET_KEY_PATTERNS entry,
# plus the historical exact-match set and the keys the audit proved leaking.
CANONICAL_SECRET_KEYS = [
    # Historical closed set (already masked before the fix)
    "authorization",
    "token",
    "password",
    "api_key",
    "secret",
    "auth",
    # Keys the audit (5.6) proved LEAKING in connection_config before A-02
    "bearer",
    "credential",
    "credentials",
    "passphrase",
    "client_secret",
    "access_key",
    # Pattern-derived variants
    "apikey",
    "openai_key",
    "key_id",
    "bearer_auth",
    "API_TOKEN",  # case-insensitivity guard
]


@pytest.mark.parametrize("key", CANONICAL_SECRET_KEYS)
def test_both_redaction_paths_mask_the_same_canonical_keys(key: str) -> None:
    """Audit 6.4b — for each canonical secret key, connection_config AND
    tool-arguments redaction must both mask. This is the test whose absence
    let the 5.6 leak pass green."""
    payload = {key: "s3cret-value"}

    via_connection_config = _redact_connection_config(dict(payload))
    via_arguments = _redact_arguments(dict(payload))

    assert via_connection_config[key] == REDACTED, f"connection_config redaction lets {key!r} leak"
    assert via_arguments[key] == REDACTED, f"arguments redaction lets {key!r} leak"


@pytest.mark.parametrize("key", CANONICAL_SECRET_KEYS)
def test_shared_predicate_covers_canonical_keys(key: str) -> None:
    """The shared predicate is the single source of truth."""
    assert is_secret_key(key)


def test_m5_arguments_redaction_is_the_shared_walker() -> None:
    """``_redact_arguments`` must BE the shared walker (alias), not a fork —
    a fork is how the original divergence appeared."""
    assert _redact_arguments is redact_recursive


@pytest.mark.parametrize("key", ["command", "args", "url", "transport", "name"])
def test_structural_keys_are_not_masked(key: str) -> None:
    """Non-secret structural keys must pass through both paths unmasked."""
    payload = {key: "structural-value"}
    assert _redact_connection_config(dict(payload))[key] == "structural-value"
    assert _redact_arguments(dict(payload))[key] == "structural-value"
