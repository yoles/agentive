"""Unitaires de la politique de sandbox par serveur — Story 5.2 T7.2 (défer D62).

L'assertion porte sur l'**argv bwrap**, pas sur un comportement observé de
bwrap. C'est le seul moyen de couvrir ce chemin ici : `bwrap` est présent dans
l'image mais le noyau refuse les user namespaces non privilégiés, donc tout
tombe en repli `setrlimit` — d'où les 4 tests skippés de
`tests/integration/mcp/test_sandbox_bypass.py` à chaque passe. Précédent exact :
`tests/unit/mcp/test_discovery_sandbox.py`, qui passe `backend="bwrap"` à la
main pour la même raison.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

import agentive_backend.infra.mcp.client as client_module
from agentive_backend.infra.mcp.client import discover_tools
from agentive_backend.infra.mcp.policy import (
    CODE_SEARCH_MODULE,
    apply_sandbox_policy,
    code_search_args,
    code_search_command,
    code_search_connection_config,
    code_search_profile,
    is_code_search_server,
)
from agentive_backend.infra.mcp.sandbox import SandboxProfile, _build_bwrap_argv
from agentive_backend.shared.config import Settings, settings


def _ro_binds_in(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == "--ro-bind"]


# ─── Le profil ───────────────────────────────────────────────────────────


def test_the_bwrap_argv_ro_binds_the_allowed_roots_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2.4 — « les racines autorisées, ET POUR AUCUNE AUTRE ».

    Le défaut de `SandboxProfile` ro-bind `/usr /etc /lib /lib64 /bin /sbin`
    et AUCUN répertoire de projet : sous bwrap, le serveur ne verrait ni le
    code à lire ni `/app/.venv`, et ne démarrerait pas. C'est ce que ce test
    garde dans un sens — et, dans l'autre, qu'on n'a pas élargi au passage.
    """
    monkeypatch.setattr(settings, "dev_code_roots_raw", "/app")

    argv = _build_bwrap_argv("x", [], profile=code_search_profile())

    assert _ro_binds_in(argv) == ["/app", "/usr", "/lib", "/lib64"]
    # `/etc` est délibérément ABSENT : le serveur n'a ni réseau (donc pas de
    # résolution DNS) ni besoin de `/etc/passwd`, et c'est exactement le genre
    # de répertoire qu'un agent de lecture n'a aucune raison de voir.
    assert "/etc" not in _ro_binds_in(argv)
    assert "--unshare-net" in argv


def test_several_roots_are_all_ro_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dev_code_roots_raw", "/app, /srv/projects ,/app")

    profile = code_search_profile()

    # Dédupliqué, et dans l'ordre déclaré — un ro-bind en double ferait
    # échouer bwrap.
    assert profile.ro_binds == ("/app", "/srv/projects", "/usr", "/lib", "/lib64")


def test_the_profile_keeps_the_network_closed() -> None:
    """Lire du code ne demande pas de réseau.

    C'est aussi ce qui distingue ce serveur de celui que la Story 5.1 avait
    jugé impossible : le sien avait besoin de Postgres, donc du réseau que
    `unshare_net=True` supprime. Celui-ci a besoin du DISQUE.
    """
    assert code_search_profile().unshare_net is True


def test_the_command_is_an_absolute_interpreter_path() -> None:
    """T2.5 — sous bwrap, `--clearenv` ne laisse que `PATH`.

    Lancer `"python"` dépendrait alors d'un `PATH` qui pointe vers le venv, ce
    qui n'est vrai que par chance. C'est le point exact où un serveur
    « enregistré avec succès » peut se révéler impossible à lancer.
    """
    command = code_search_command()
    assert command.startswith("/")


def test_the_interpreter_lives_under_an_allowed_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sinon le sandbox ne le voit pas, et le serveur ne démarre pas du tout.

    Cette contrainte n'est écrite nulle part ailleurs, et elle est invisible
    sur cette machine : le repli `setrlimit` ne monte rien, donc tout marche
    localement et rien ne marcherait en production.
    """
    monkeypatch.setattr(settings, "dev_code_roots_raw", "/app")
    command = code_search_command()
    roots = (*settings.dev_code_roots, "/usr", "/lib", "/lib64")
    assert any(command.startswith(f"{root}/") for root in roots), (
        f"l'interpréteur {command} n'est sous aucune racine ro-bindée {roots} — "
        "sous bwrap, le serveur ne pourrait pas être exécuté"
    )


# ─── La réécriture de la configuration de connexion ──────────────────────


def test_the_allowlist_comes_from_settings_not_from_the_stored_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2.3 — « dans `Settings`, pas dans une colonne ».

    `tool_servers.connection_config` est une colonne : un `UPDATE` suffirait à
    y écrire `--root /`. Une allowlist de chemins qui décide de ce qu'un agent
    LLM peut lire doit être lisible dans un diff git et revue comme du code.
    La politique la RÉÉCRIT à chaque appel, donc la colonne ne décide de rien.
    """
    monkeypatch.setattr(settings, "dev_code_roots_raw", "/app")
    # Revue 5.2 — la version d'origine mettait ici le `command` LÉGITIME, donc
    # le seul champ que la politique ne réécrivait pas n'était jamais altéré
    # par le test : il validait sa propre fixture sur cet axe.
    tampered = {
        "command": "/bin/sh",
        "args": ["-m", CODE_SEARCH_MODULE, "--root", "/"],
        "env": {"ANTHROPIC_API_KEY": "sk-ant-leaked"},
    }

    profile, effective = apply_sandbox_policy(transport="stdio", connection_config=tampered)

    assert effective["args"].count("--root") == 1
    assert effective["args"][effective["args"].index("--root") + 1] == "/app"
    assert profile is not None
    assert "/" not in profile.ro_binds
    # Et l'`env` trafiqué ne survit pas non plus : il est remplacé, pas filtré.
    assert "ANTHROPIC_API_KEY" not in effective["env"]
    # Ni le `command` : c'est ce que la docstring du module promettait déjà
    # (« un `UPDATE` sur cette colonne ne peut pas élargir l'allowlist ») sans
    # que le code le fasse. Le binaire exécuté restait décidé par la row.
    assert effective["command"] == code_search_command()


def test_the_rewritten_env_makes_the_stored_column_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renommé et re-justifié par la revue 5.2.

    L'ancien nom — `..._so_the_subprocess_stops_inheriting` — nommait une
    propriété qui n'existe pas. Il n'y a PAS de fuite d'environnement à
    fermer : `client.py` passe `StdioServerParameters(env=None)` quand aucun
    `env` n'est déclaré, et le SDK MCP applique alors
    `get_default_environment()`, une allowlist qui rend `{HOME, PATH}` — pas
    l'environnement du backend. `sandboxed_subprocess`, citée comme le
    mécanisme fautif par le piège #12 de la story, n'est appelée par aucun code
    de production.

    Ce que cette réécriture garantit réellement est l'INTÉGRITÉ : la colonne
    `connection_config.env` ne décide de rien, exactement comme `args`.
    """
    monkeypatch.setattr(settings, "dev_code_roots_raw", "/app")
    _, effective = apply_sandbox_policy(
        transport="stdio", connection_config=code_search_connection_config()
    )

    assert set(effective["env"]) == {"PATH"}


def test_a_server_whose_argv_merely_mentions_the_module_is_not_recognised() -> None:
    """Revue 5.2 — la reconnaissance testait une APPARTENANCE, sans position.

    Une row tierce dont les `args` contenaient la chaîne n'importe où était
    reconnue comme le serveur de lecture de code : elle recevait son profil
    (donc les racines ro-bindées) tout en gardant son propre `command`.
    """
    impostor = {
        "command": "npx",
        "args": ["--config", CODE_SEARCH_MODULE],
    }
    profile, effective = apply_sandbox_policy(transport="stdio", connection_config=impostor)

    assert profile is None
    assert effective is impostor


def test_an_interpreter_that_is_not_an_absolute_path_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2.5 disait « chemin ABSOLU, et vérifié » — rien n'était vérifié."""
    monkeypatch.setattr("agentive_backend.infra.mcp.policy.sys.executable", "")
    with pytest.raises(RuntimeError, match="chemin absolu"):
        code_search_command()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/app", ("/app",)),
        ("/app/", ("/app",)),
        ("/app,/app/", ("/app",)),
        ("/app//src", ("/app/src",)),
        ("  /app  ,  /srv/code  ", ("/app", "/srv/code")),
    ],
)
def test_the_roots_are_normalised_before_they_become_ro_binds(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: tuple[str, ...]
) -> None:
    """Revue 5.2 — la déduplication portait sur la CHAÎNE EXACTE.

    `/app,/app/` produisait donc deux entrées et deux `--ro-bind`, que bwrap
    refuse. Le test qui gardait cette propriété n'exerçait que le doublon
    exact, seul cas que la déduplication attrapait.
    """
    monkeypatch.setattr(settings, "dev_code_roots_raw", raw)
    assert settings.dev_code_roots == expected
    profile = code_search_profile()
    assert len(profile.ro_binds) == len(set(profile.ro_binds))


@pytest.mark.parametrize("raw", ["/app/..", "/./..", "/app/../etc"])
def test_a_root_that_climbs_out_is_refused_at_startup(raw: str) -> None:
    """`'/app/..'.rstrip('/')` ne vaut ni `''` ni `/`, donc il passait.

    Or il RÉSOUT vers `/`, et `code_search_profile` ro-bindait la chaîne brute :
    le sandbox montait tout le filesystem (revue 5.2). La valeur lue dans un
    diff git doit être celle qui s'applique.
    """
    with pytest.raises(ValidationError, match=r"\.\.|allowlist"):
        Settings(AGENTIVE_DEV_CODE_ROOTS=raw)  # type: ignore[call-arg]


def test_the_bounds_travel_by_argv_not_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--clearenv` et le filtre `env_passthrough` feraient disparaître une
    variable, et le serveur retomberait en silence sur ses défauts de module."""
    monkeypatch.setattr(settings, "dev_code_max_read_bytes", 1_234)
    monkeypatch.setattr(settings, "dev_code_max_results", 7)
    monkeypatch.setattr(settings, "dev_code_max_depth", 3)

    args = code_search_args()

    assert args[args.index("--max-read-bytes") + 1] == "1234"
    assert args[args.index("--max-results") + 1] == "7"
    assert args[args.index("--max-depth") + 1] == "3"


def test_a_server_the_policy_does_not_know_is_left_exactly_as_it_was() -> None:
    """La politique AJOUTE une règle pour un serveur nommé ; elle ne change
    rien pour les autres — le comportement d'avant cette story, octet pour
    octet."""
    other = {"command": "npx", "args": ["-y", "@acme/some-mcp-server"]}

    profile, effective = apply_sandbox_policy(transport="stdio", connection_config=other)

    assert profile is None
    assert effective is other


def test_an_sse_server_is_never_given_a_filesystem_profile() -> None:
    """SSE ne spawne aucun sous-processus : il n'y a rien de local à confiner,
    et `call_tool` fait déjà cette exemption."""
    profile, _ = apply_sandbox_policy(
        transport="sse", connection_config={"url": "https://mcp.example.test/sse"}
    )
    assert profile is None


def test_the_server_is_recognised_by_its_module_not_by_its_row_name() -> None:
    """Le `name` de la row est choisi par le catalogue et peut être renommé ;
    le module lancé, lui, est ce que le serveur EST."""
    assert is_code_search_server({"args": ["-m", CODE_SEARCH_MODULE]}) is True
    assert is_code_search_server({"args": ["-m", "some.other.module"]}) is False
    assert is_code_search_server({"command": "dev-code-search"}) is False
    assert is_code_search_server(None) is False


# ─── Le profil atteint VRAIMENT le spawn ─────────────────────────────────


@pytest.fixture
def captured_params(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture les `StdioServerParameters` que le SDK recevrait.

    Même fixture que `test_discovery_sandbox.py` : c'est là que la décision de
    confinement devient observable sans binaire bwrap fonctionnel.
    """
    captured: list[Any] = []

    class _FakeStdioClient:
        def __init__(self, params: Any) -> None:
            captured.append(params)

        async def __aenter__(self) -> tuple[Any, Any]:
            return MagicMock(), MagicMock()

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    async def _fake_list_tools(_read: Any, _write: Any) -> list[Any]:
        return []

    monkeypatch.setattr(client_module, "stdio_client", _FakeStdioClient)
    monkeypatch.setattr(client_module, "_list_tools_via_session", _fake_list_tools)
    return captured


@pytest.mark.asyncio
async def test_a_profile_passed_to_discovery_reaches_the_bwrap_argv(
    captured_params: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le constat qui a motivé T2 : AUCUN appelant ne passait `profile=`.

    Le profil pouvait donc être parfaitement calculé et n'atteindre jamais le
    spawn — exactement le défaut P2 de la revue 5.0 (`node_tools` câblé par
    aucun appelant de production), qui passait tous les tests unitaires.
    """
    monkeypatch.setattr(settings, "dev_code_roots_raw", "/app")
    profile, effective = apply_sandbox_policy(
        transport="stdio", connection_config=code_search_connection_config()
    )

    await discover_tools(
        transport="stdio",
        connection_config=effective,
        profile=profile,
        backend="bwrap",
    )

    params = captured_params[0]
    assert params.command == "bwrap"
    assert _ro_binds_in(params.args) == ["/app", "/usr", "/lib", "/lib64"]
    assert CODE_SEARCH_MODULE in params.args


@pytest.mark.asyncio
async def test_a_caller_that_forgets_the_profile_still_gets_the_policy(
    captured_params: list[Any],
) -> None:
    """⚠️ Ce test était le NÉGATIF du précédent. Il ne peut plus l'être.

    Il asserait que, sans profil explicite, `SandboxProfile()` s'applique et
    qu'aucune racine de projet n'est ro-bindée — donc que sous bwrap le serveur
    ne verrait ni le code ni son propre interpréteur. C'était vrai, et c'était
    le problème : la garantie affichée par `mcp-servers.yaml` (« un `UPDATE`
    sur `connection_config` ne peut pas élargir l'allowlist ») reposait
    entièrement sur le fait qu'aucun appelant n'oublie `profile=`. La revue a
    relevé que le commentaire de `mise_en_place` le disait sans le voir :
    « TROISIÈME appelant, et le plus facile à oublier ».

    `infra/mcp/client.py` applique donc la politique lui-même, au point de
    passage obligé. Un quatrième appelant distrait est désormais couvert, et
    c'est cette propriété-là que ce test garde.
    """
    await discover_tools(
        transport="stdio",
        connection_config={"command": "/bin/sh", "args": ["-m", CODE_SEARCH_MODULE]},
        backend="bwrap",
    )

    ro_binds = _ro_binds_in(captured_params[0].args)
    assert ro_binds == ["/app", "/usr", "/lib", "/lib64"]
    assert ro_binds != list(SandboxProfile().ro_binds)
    # Et le `command` de la configuration est repris en main lui aussi.
    assert "/bin/sh" not in captured_params[0].args
