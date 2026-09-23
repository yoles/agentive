"""Unitaires du serveur MCP de lecture de code — Story 5.2 T7.1.

Chaque test est nommé par la PROPRIÉTÉ qu'il garde, pas par la fonction qu'il
appelle : la garde de chemin et les bornes sont des règles de sécurité, et une
règle se teste par ce qu'elle interdit.

Ces tests appellent `dispatch` plutôt que le protocole MCP : ce qui est vérifié
ici est la décision (refuser, borner, dire qu'on a tronqué), et la traduction
`exception → CallToolResult.isError` est vérifiée par
`tests/integration/mcp/test_code_search_server_e2e.py`, sur le vrai transport.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import pytest

from agentive_backend.infra.mcp.servers.code_search import (
    DENIED_NAME_PATTERNS,
    MAX_MATCH_LINE_CHARS,
    CodeSearchGuard,
    CodeSearchLimits,
    PathRefusedError,
    dispatch,
    tool_definitions,
)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Un faux dépôt : deux « modules », un secret, un fichier hors racine."""
    root = tmp_path / "repo"
    (root / "features" / "alpha").mkdir(parents=True)
    (root / "features" / "beta").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / ".git").mkdir()

    (root / "features" / "alpha" / "router.py").write_text(
        "class AlphaRouter:\n    pass\n", encoding="utf-8"
    )
    (root / "features" / "alpha" / "service.py").write_text(
        "class AlphaService:\n    pass\n", encoding="utf-8"
    )
    (root / "features" / "beta" / "router.py").write_text(
        "class BetaRouter:\n    pass\n", encoding="utf-8"
    )
    (root / ".env").write_text("POSTGRES_PASSWORD=hunter2\n", encoding="utf-8")
    (root / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (root / "secrets" / "token.txt").write_text("sk-ant-real\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[remote]\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.txt").write_text("hors racine\n", encoding="utf-8")
    return root


@pytest.fixture
def guard(corpus: Path) -> CodeSearchGuard:
    return CodeSearchGuard.from_paths([corpus])


@pytest.fixture
def limits() -> CodeSearchLimits:
    return CodeSearchLimits()


# ─── La garde de chemin ──────────────────────────────────────────────────


def test_a_path_that_climbs_out_with_dotdot_is_refused(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    with pytest.raises(PathRefusedError) as exc:
        dispatch(guard, limits, "read_file", {"path": "../outside/stolen.txt"})
    assert "hors des racines autorisées" in str(exc.value)


def test_an_absolute_path_outside_the_roots_is_refused(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    target = corpus.parent / "outside" / "stolen.txt"
    with pytest.raises(PathRefusedError):
        dispatch(guard, limits, "read_file", {"path": str(target)})


def test_a_symlink_pointing_out_of_the_roots_is_refused(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    """Le cas qui distingue un contrôle VRAI d'un contrôle cosmétique.

    Un test de préfixe sur la chaîne passerait : le lien est bien SOUS la
    racine. `Path.resolve()` suit le lien, donc la comparaison porte sur la
    cible réelle — et c'est la seule version du contrôle qui tient.
    """
    link = corpus / "innocent.txt"
    link.symlink_to(corpus.parent / "outside" / "stolen.txt")

    with pytest.raises(PathRefusedError) as exc:
        dispatch(guard, limits, "read_file", {"path": str(link)})
    assert "hors des racines autorisées" in str(exc.value)


def test_a_symlink_pointing_out_of_the_roots_never_appears_in_results(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    """Refuser une lecture ne suffit pas si un parcours le rend quand même.

    Un `find_files` qui liste le lien donne au modèle un chemin qu'il citera
    dans `relevant_files` — et l'AC2 promet des chemins vérifiables.
    """
    link = corpus / "features" / "alpha" / "leak.py"
    link.symlink_to(corpus.parent / "outside" / "stolen.txt")

    result = dispatch(guard, limits, "find_files", {"pattern": "*"})
    assert all("leak.py" not in match for match in result["matches"])


@pytest.mark.parametrize(
    "relative",
    [".env", "deploy.pem", "secrets/token.txt", ".git/config"],
)
def test_the_deny_list_refuses_reading_a_secret(
    guard: CodeSearchGuard, limits: CodeSearchLimits, relative: str
) -> None:
    """Sous le repli `setrlimit` il n'y a AUCUNE isolation filesystem.

    Cette liste est alors la seule frontière qui existe À L'INTÉRIEUR d'une
    racine autorisée. Le fait que le container ne monte aujourd'hui que
    `./backend` met `.env` hors de portée par ACCIDENT de configuration, pas
    par conception : un opérateur qui élargit `AGENTIVE_DEV_CODE_ROOTS`
    annulerait cette protection-là sans le savoir. Celle-ci voyage avec le
    serveur.
    """
    with pytest.raises(PathRefusedError) as exc:
        dispatch(guard, limits, "read_file", {"path": relative})
    assert "deny-list" in str(exc.value)


def test_the_deny_list_also_filters_search_results(
    guard: CodeSearchGuard, limits: CodeSearchLimits
) -> None:
    """T1.5 — « aux lectures ET aux résultats de recherche ».

    Refuser la lecture de `.env` tout en laissant `search_content` rendre la
    ligne `POSTGRES_PASSWORD=hunter2` rendrait la garde décorative : le secret
    est DANS le résultat de recherche, pas seulement derrière une lecture.
    """
    found = dispatch(guard, limits, "search_content", {"pattern": "hunter2|sk-ant|BEGIN PRIVATE"})
    assert found["matches"] == [], found["matches"]

    listed = dispatch(guard, limits, "find_files", {"pattern": "*"})
    joined = " ".join(listed["matches"])
    assert ".env" not in joined
    assert ".pem" not in joined
    assert "secrets" not in joined


def test_a_denied_directory_is_absent_from_a_listing(
    guard: CodeSearchGuard, limits: CodeSearchLimits
) -> None:
    entries = dispatch(guard, limits, "list_directory", {})
    names = {entry["name"] for entry in entries["entries"]}
    assert "features" in names
    assert names.isdisjoint({".env", ".git", "secrets", "deploy.pem"})


#: Noms qui DOIVENT être refusés — écrits indépendamment de
#: `DENIED_NAME_PATTERNS`, et c'est tout l'intérêt (revue 5.2).
#:
#: L'ancienne version dérivait ses échantillons des motifs eux-mêmes, puis
#: vérifiait que chaque échantillon matchait son propre motif : elle validait
#: sa fixture. Elle ne pouvait par construction jamais révéler un nom sensible
#: ABSENT de la liste — et `.envrc`, `staging.env`, `Secrets/` et `DEPLOY.PEM`
#: étaient effectivement lisibles.
MUST_BE_DENIED = (
    ".env",
    ".env.local",
    ".envrc",
    ".env_backup",
    "staging.env",
    "prod.env",
    ".ENV",
    ".git",
    ".ssh",
    ".aws",
    "server.pem",
    "DEPLOY.PEM",
    "private.key",
    "client.crt",
    "bundle.p12",
    "id_rsa",
    "id_ed25519",
    "db.sqlite",
    "db.sqlite3",
    "secrets",
    "Secrets",
    "secrets.yaml",
    ".secrets",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    ".gnupg",
    "backup.pfx",
    "store.jks",
)

#: Noms qui doivent RESTER lisibles — la contrepartie, sans laquelle une
#: deny-list trop large passerait le test précédent en refusant tout.
MUST_STAY_READABLE = (
    "router.py",
    "service.py",
    "README.md",
    "environment.py",
    "envelope.ts",
    "keyboard.py",
    "identity.py",
    "secretary.md",
)


@pytest.mark.parametrize("name", MUST_BE_DENIED)
def test_a_secret_bearing_name_is_denied(name: str) -> None:
    """La deny-list est écrite contre une liste de noms, pas contre elle-même."""
    root = Path("/allow")
    assert CodeSearchGuard.denied_component(root / name / "x", root) is not None, name


@pytest.mark.parametrize("name", MUST_STAY_READABLE)
def test_an_ordinary_source_name_is_not_denied(name: str) -> None:
    """Contrepartie : une deny-list qui refuse tout passerait l'autre test."""
    root = Path("/allow")
    assert CodeSearchGuard.denied_component(root / name, root) is None, name


def test_every_denied_pattern_is_exercised_by_a_sample() -> None:
    """Aucun motif décoratif : chaque entrée de la liste doit attraper un nom réel."""
    unmatched = [
        pattern
        for pattern in DENIED_NAME_PATTERNS
        if not any(fnmatch.fnmatch(name.lower(), pattern) for name in MUST_BE_DENIED)
    ]
    assert unmatched == [], f"motifs sans échantillon dans MUST_BE_DENIED : {unmatched}"


def test_a_root_whose_own_path_is_denied_is_refused(tmp_path: Path) -> None:
    """Une racine `/…/secrets` rendait tout son sous-arbre lisible.

    `denied_component` n'inspecte que le chemin RELATIF à la racine : aucun
    composant relatif ne porte alors le nom interdit (revue 5.2).
    """
    root = tmp_path / "secrets"
    root.mkdir()
    with pytest.raises(ValueError, match="deny-list"):
        CodeSearchGuard.from_paths([str(root)])


def test_a_relative_root_is_refused_by_the_server_itself(tmp_path: Path) -> None:
    """`Path("").resolve()` rend le cwd, que le modèle ne contrôle ni n'observe.

    Le refus existait dans `Settings`, pas dans le serveur — alors que celui-ci
    est lançable en direct par `python -m` (revue 5.2).
    """
    with pytest.raises(ValueError, match="relative"):
        CodeSearchGuard.from_paths(["relatif/ici"])
    with pytest.raises(ValueError, match="vide"):
        CodeSearchGuard.from_paths([""])


def test_a_root_that_is_the_filesystem_root_is_refused() -> None:
    """Une allowlist qui contient `/` n'est pas une allowlist."""
    with pytest.raises(ValueError, match="trop large"):
        CodeSearchGuard.from_paths(["/"])


def test_no_root_at_all_is_refused() -> None:
    with pytest.raises(ValueError, match="au moins une racine"):
        CodeSearchGuard.from_paths([])


# ─── Les bornes ──────────────────────────────────────────────────────────


def test_a_truncated_read_says_so_and_offers_the_next_offset(
    guard: CodeSearchGuard, corpus: Path
) -> None:
    """Un résultat coupé en SILENCE fait croire au modèle qu'il a tout vu.

    C'est le défaut que T1.6 nomme : `MAX_TOOL_RESULT_CHARS` tronque déjà,
    mais après lecture et sans le dire au niveau de l'outil. Ici la troncature
    est annoncée ET reprenable.
    """
    big = corpus / "features" / "alpha" / "huge.py"
    big.write_text("x" * 5_000, encoding="utf-8")
    limits = CodeSearchLimits(max_read_bytes=100)

    first = dispatch(guard, limits, "read_file", {"path": str(big)})
    assert first["truncated"] is True
    assert first["bytes_returned"] == 100
    assert first["total_bytes"] == 5_000
    assert first["next_offset"] == 100
    assert "TRONQUÉ" in first["note"]

    second = dispatch(
        guard, limits, "read_file", {"path": str(big), "offset": first["next_offset"]}
    )
    assert second["offset"] == 100
    assert second["bytes_returned"] == 100


def test_a_complete_read_carries_no_truncation_note(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    """La PRÉSENCE de `note` est le signal — même posture que
    `contract_problems` dans `metrics.per_node`."""
    result = dispatch(guard, limits, "read_file", {"path": "features/alpha/router.py"})
    assert result["truncated"] is False
    assert "note" not in result


def test_the_result_ceiling_is_applied_at_the_source(guard: CodeSearchGuard, corpus: Path) -> None:
    for index in range(30):
        (corpus / "features" / "alpha" / f"mod{index}.py").write_text("pass\n", encoding="utf-8")
    limits = CodeSearchLimits(max_results=5)

    result = dispatch(guard, limits, "find_files", {"pattern": "*.py"})
    assert len(result["matches"]) == 5
    assert result["truncated"] is True
    assert "TRONQUÉ" in result["note"]


def test_a_caller_cannot_raise_the_ceiling_above_the_configured_one(
    guard: CodeSearchGuard, corpus: Path
) -> None:
    """`max_results` est un plafond, pas une suggestion.

    Le modèle choisit l'argument ; s'il pouvait demander 10 000 résultats, la
    borne d'exploitation n'en serait plus une.
    """
    for index in range(30):
        (corpus / "features" / "beta" / f"mod{index}.py").write_text("pass\n", encoding="utf-8")
    limits = CodeSearchLimits(max_results=4)

    result = dispatch(guard, limits, "find_files", {"pattern": "*.py", "max_results": 10_000})
    assert len(result["matches"]) == 4


def test_the_walk_stops_at_the_configured_depth(guard: CodeSearchGuard, corpus: Path) -> None:
    deep = corpus / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "buried.py").write_text("pass\n", encoding="utf-8")

    shallow = dispatch(guard, CodeSearchLimits(max_depth=2), "find_files", {"pattern": "buried.py"})
    assert shallow["matches"] == []
    # Revue 5.2 — ce test ÉPINGLAIT le résultat vide silencieux que T1.6
    # interdit. Un parcours coupé par la profondeur doit le DIRE.
    assert shallow["truncated"] is True
    assert "profondeur" in shallow["note"]

    reachable = dispatch(
        guard, CodeSearchLimits(max_depth=10), "find_files", {"pattern": "buried.py"}
    )
    assert len(reachable["matches"]) == 1


def test_a_very_long_matching_line_is_cut_and_flagged(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    """Un fichier minifié sur une seule ligne remplirait la réponse à lui seul."""
    (corpus / "features" / "alpha" / "bundle.js").write_text(
        "NEEDLE" + "z" * 5_000 + "\n", encoding="utf-8"
    )
    result = dispatch(guard, limits, "search_content", {"pattern": "NEEDLE"})
    assert len(result["matches"]) == 1
    assert len(result["matches"][0]["text"]) == MAX_MATCH_LINE_CHARS
    assert result["matches"][0]["line_truncated"] is True


def test_the_clamp_refuses_a_ceiling_larger_than_the_hard_maximum() -> None:
    clamped = CodeSearchLimits.clamped(
        max_read_bytes=10_000_000, max_results=10_000_000, max_depth=10_000
    )
    assert clamped.max_read_bytes == 200_000
    assert clamped.max_results == 1_000
    assert clamped.max_depth == 40


# ─── Lecture seule, et rien d'autre ──────────────────────────────────────


def test_the_server_exposes_read_tools_and_nothing_else() -> None:
    """AC1 — « tous en lecture seule par construction ».

    Un outil générique d'exécution transformerait la fuite d'environnement du
    repli `setrlimit` (piège #12 de la story : `env=effective_env or None`) en
    exfiltration. La garde est structurelle : la liste est close.
    """
    names = {tool.name for tool in tool_definitions()}
    assert names == {"list_directory", "read_file", "find_files", "search_content"}


def test_an_unknown_tool_name_is_refused_rather_than_ignored(
    guard: CodeSearchGuard, limits: CodeSearchLimits
) -> None:
    with pytest.raises(ValueError, match="outil inconnu"):
        dispatch(guard, limits, "write_file", {"path": "x", "content": "y"})


def test_the_module_never_opens_a_file_for_writing() -> None:
    """Garde STRUCTURELLE plutôt que revue humaine.

    « Lecture seule par construction » est la propriété qui justifie que ce
    serveur existe plutôt qu'un serveur tiers ; la laisser reposer sur la
    vigilance d'un relecteur serait la même erreur que de compter sur « ne pas
    oublier `wrap_external_input` » — un oubli qui a survécu deux epics
    (Story 9.7).

    L'analyse porte sur l'AST et non sur le texte : le module PARLE de
    `subprocess` dans sa docstring pour dire qu'il n'en fait pas, et un
    `assert "subprocess" not in source` échouerait sur sa propre
    documentation.
    """
    import ast

    from agentive_backend.infra.mcp.servers import code_search

    tree = ast.parse(Path(code_search.__file__).read_text(encoding="utf-8"))

    forbidden_modules = {"subprocess", "shutil", "socket", "shlex", "pty"}
    forbidden_attributes = {"system", "popen", "remove", "unlink", "rmdir", "rename", "chmod"}
    read_modes = {"r", "rb", "rt"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_modules, node.module
        elif isinstance(node, ast.Attribute):
            # `os.system`, `os.remove`, `Path.unlink`… — le nom de l'attribut
            # suffit, quel que soit le porteur.
            assert node.attr not in forbidden_attributes, node.attr
        elif isinstance(node, ast.Call):
            callee = node.func
            builtin_open = isinstance(callee, ast.Name) and callee.id == "open"
            method_open = isinstance(callee, ast.Attribute) and callee.attr == "open"
            if not (builtin_open or method_open):
                continue
            # `open(path, mode)` porte le mode en second argument ;
            # `path.open(mode)` en premier.
            positional = node.args[1:] if builtin_open else node.args
            modes = [
                arg.value
                for arg in positional
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            modes += [
                kw.value.value
                for kw in node.keywords
                if kw.arg == "mode"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ]
            assert modes, "un `open()` sans mode explicite — le défaut est `r`, mais dis-le"
            for mode in modes:
                assert mode in read_modes, f"mode d'ouverture non lecture : {mode!r}"


def test_a_relative_path_is_resolved_against_the_first_root_not_the_cwd(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    """Le répertoire courant d'un sous-processus sandboxé n'est pas une notion
    que le modèle contrôle ni observe."""
    previous = Path.cwd()
    os.chdir(corpus.parent)
    try:
        result = dispatch(guard, limits, "read_file", {"path": "features/alpha/router.py"})
    finally:
        os.chdir(previous)
    assert result["path"] == str(corpus / "features" / "alpha" / "router.py")
    assert "AlphaRouter" in result["content"]


def test_an_over_long_regex_is_refused(guard: CodeSearchGuard, limits: CodeSearchLimits) -> None:
    """Renommé (revue 5.2) : ce test garde la LONGUEUR, pas la pathologie.

    L'ancien nom — `..._pathological_regex_...` — affirmait une propriété que
    le code ne tenait pas : `(a+)+$` fait 7 caractères et passait.
    """
    with pytest.raises(PathRefusedError, match="motif trop long"):
        dispatch(guard, limits, "search_content", {"pattern": "a" * 501})


@pytest.mark.parametrize("pattern", ["(a+)+$", "(a*)*b", r"(\w+)+@", "(x+x+)+y"])
def test_a_nested_quantifier_is_refused_before_it_runs(
    guard: CodeSearchGuard, limits: CodeSearchLimits, pattern: str
) -> None:
    """Un backtracking exponentiel tient en 7 caractères, sous le plafond de longueur."""
    with pytest.raises(PathRefusedError, match="quantificateurs imbriqués"):
        dispatch(guard, limits, "search_content", {"pattern": pattern})


def test_a_regex_that_overflows_is_an_exploitable_error_not_a_crash(
    guard: CodeSearchGuard, limits: CodeSearchLimits
) -> None:
    """`a{4294967296}` lève `OverflowError`, que `except re.error` ne rattrapait pas."""
    with pytest.raises(PathRefusedError, match="expression régulière invalide"):
        dispatch(guard, limits, "search_content", {"pattern": "a{4294967296}"})


def test_an_invalid_regex_is_an_exploitable_error_not_a_crash(
    guard: CodeSearchGuard, limits: CodeSearchLimits
) -> None:
    with pytest.raises(PathRefusedError, match="expression régulière invalide"):
        dispatch(guard, limits, "search_content", {"pattern": "(unclosed"})


# ─── Revue 5.2 — « l'incomplétude est DITE » ──────────────────────────────
#
# Le module s'interdit en tête de fichier de couper en silence, et le faisait
# sur quatre chemins. Un test par chemin, chacun sur la propriété qu'il garde.


def test_the_scan_budget_says_so_when_it_cuts(tmp_path: Path) -> None:
    """Le défaut le plus grave de la revue : `find_files` certifiait complet.

    `_iter_files` faisait un `return` nu quand le budget de balayage était
    épuisé — indiscernable d'une fin de parcours normale. Sur un corpus plus
    large que `MAX_FILES_SCANNED`, un fichier qui EXISTE était rendu
    `count=0, truncated=false` : « je n'ai rien trouvé » sur une troncature.
    """
    from agentive_backend.infra.mcp.servers.code_search import MAX_FILES_SCANNED

    (tmp_path / "x").mkdir()
    for i in range(MAX_FILES_SCANNED + 20):
        (tmp_path / "x" / f"f{i:05d}.txt").write_text("rien\n", encoding="utf-8")
    (tmp_path / "x" / "zzz_needle.txt").write_text("NEEDLE\n", encoding="utf-8")

    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    result = dispatch(guard, CodeSearchLimits(), "find_files", {"pattern": "zzz_needle.txt"})

    assert result["matches"] == []
    assert result["truncated"] is True
    assert "budget de balayage" in result["note"]


def test_an_unreadable_directory_is_counted_rather_than_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.walk` avale les `EACCES` par défaut : l'incomplétude était invisible.

    Testé en déclenchant le `onerror` de `os.walk` plutôt qu'en posant un
    `chmod 000` — le container tourne en root, qui traverse un répertoire
    interdit. Un test qui se skippe dans l'environnement où il tourne ne garde
    rien.
    """
    (tmp_path / "visible.py").write_text("NEEDLE\n", encoding="utf-8")
    real_walk = os.walk

    def _walk_with_one_failure(top, topdown=True, onerror=None, followlinks=False):  # type: ignore[no-untyped-def]
        if onerror is not None:
            onerror(PermissionError(13, "Permission denied", str(tmp_path / "locked")))
        yield from real_walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks)

    monkeypatch.setattr(os, "walk", _walk_with_one_failure)
    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    result = dispatch(guard, CodeSearchLimits(), "search_content", {"pattern": "NEEDLE"})

    assert result["matches"], "le fichier lisible doit toujours être trouvé"
    assert result["truncated"] is True
    assert "illisible" in result["note"]


def test_a_file_too_large_to_scan_is_reported_not_skipped_in_silence(tmp_path: Path) -> None:
    from agentive_backend.infra.mcp.servers.code_search import MAX_SCAN_FILE_BYTES

    big = tmp_path / "dump.txt"
    big.write_text("x" * (MAX_SCAN_FILE_BYTES + 10), encoding="utf-8")
    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    result = dispatch(guard, CodeSearchLimits(), "search_content", {"pattern": "xxx"})
    assert result["truncated"] is True
    assert "volumineux" in result["note"]


def test_the_traversal_noise_list_keeps_the_budget_for_the_real_code(tmp_path: Path) -> None:
    """`.venv` n'était pas écarté, et la racine de PRODUCTION est `/app`.

    `docker-compose.yml` y monte `backend_venv:/app/.venv`, et `os.walk` trie
    les noms : `.venv` passait avant `src`. Une recherche depuis la racine
    brûlait son budget dans le venv (revue 5.2).
    """
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mine.py").write_text("NEEDLE\n", encoding="utf-8")

    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    result = dispatch(guard, CodeSearchLimits(), "search_content", {"pattern": "NEEDLE"})

    paths = [match["path"] for match in result["matches"]]
    assert paths == [str(tmp_path / "src" / "mine.py")]


def test_a_denied_name_is_matched_regardless_of_case(tmp_path: Path) -> None:
    """`fnmatch` passe par `os.path.normcase`, identité sous POSIX."""
    (tmp_path / "Secrets").mkdir()
    (tmp_path / "Secrets" / "t.txt").write_text("hunter2\n", encoding="utf-8")
    (tmp_path / "DEPLOY.PEM").write_text("-----BEGIN\n", encoding="utf-8")
    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    for path in ("Secrets/t.txt", "DEPLOY.PEM"):
        with pytest.raises(PathRefusedError, match="deny-list"):
            dispatch(guard, CodeSearchLimits(), "read_file", {"path": str(tmp_path / path)})


def test_a_fifo_does_not_hang_the_scan(tmp_path: Path) -> None:
    """`_size_of` rend 0 sur un FIFO, et `open("r")` bloquait sans écrivain."""
    os.mkfifo(tmp_path / "pipe.fifo")
    (tmp_path / "real.py").write_text("NEEDLE\n", encoding="utf-8")
    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    result = dispatch(guard, CodeSearchLimits(), "search_content", {"pattern": "NEEDLE"})
    assert [match["path"] for match in result["matches"]] == [str(tmp_path / "real.py")]


def test_reading_past_the_end_of_a_file_is_refused_not_rendered_empty(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    """`content: ""` + `truncated: false` était indiscernable d'un fichier vide."""
    target = corpus / "features" / "alpha" / "router.py"
    with pytest.raises(PathRefusedError, match="au-delà de la fin"):
        dispatch(guard, limits, "read_file", {"path": str(target), "offset": 10_000_000})


def test_paging_a_utf8_file_never_loses_a_character_at_the_seam(tmp_path: Path) -> None:
    """`offset`/`max_bytes` sont en OCTETS : le caractère à cheval était perdu.

    Il devenait `U+FFFD` des DEUX côtés — fin de page N et début de page N+1.
    Sur un dépôt dont les docstrings sont en français, chaque frontière en
    perdait un (revue 5.2).
    """
    target = tmp_path / "accents.txt"
    target.write_text("é" * 10, encoding="utf-8")
    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    limits = CodeSearchLimits(max_read_bytes=3)

    rebuilt = ""
    offset = 0
    while True:
        page = dispatch(guard, limits, "read_file", {"path": str(target), "offset": offset})
        rebuilt += page["content"]
        if not page["truncated"]:
            break
        offset = page["next_offset"]

    assert rebuilt == "é" * 10
    assert "\ufffd" not in rebuilt


def test_a_glob_star_does_not_cross_a_directory_separator(tmp_path: Path) -> None:
    """`fnmatch` traduit `*` en `.*`, qui traverse les `/` (revue 5.2).

    Conséquence : il n'existait AUCUN motif exprimant « ce répertoire
    seulement », et le `**/router.py` donné en exemple dans la description de
    l'outil ne matchait aucun fichier de premier niveau.
    """
    (tmp_path / "top.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "router.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "router.py").write_text("pass\n", encoding="utf-8")
    guard = CodeSearchGuard.from_paths([str(tmp_path)])
    limits = CodeSearchLimits()

    # Sans `/` : le motif porte sur le NOM, donc partout sous la racine.
    by_name = dispatch(guard, limits, "find_files", {"pattern": "*.py"})
    assert len(by_name["matches"]) == 3

    # Avec `/` : chemin relatif, et `*` ne traverse PAS les `/`.
    scoped = dispatch(guard, limits, "find_files", {"pattern": "pkg/*.py"})
    assert [Path(p).name for p in scoped["matches"]] == ["router.py"]

    # `**/` descend, et matche AUSSI le premier niveau — ce que l'ancien
    # `fnmatch` ne faisait pas, alors que la description le promettait.
    deep = dispatch(guard, limits, "find_files", {"pattern": "**/router.py"})
    assert len(deep["matches"]) == 2


def test_a_malformed_argument_stays_inside_the_error_contract(
    guard: CodeSearchGuard, limits: CodeSearchLimits, corpus: Path
) -> None:
    """`int("abc")` levait un `ValueError` nu, hors du contrat `PathRefusedError`."""
    target = str(corpus / "features" / "alpha" / "router.py")
    with pytest.raises(PathRefusedError, match="entier"):
        dispatch(guard, limits, "read_file", {"path": target, "offset": "abc"})
    with pytest.raises(PathRefusedError, match="chaîne"):
        dispatch(guard, limits, "list_directory", {"path": 123})
    with pytest.raises(PathRefusedError, match="vide"):
        dispatch(guard, limits, "list_directory", {"path": ""})
