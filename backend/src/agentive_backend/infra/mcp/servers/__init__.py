"""Serveurs MCP **internes**, écrits dans ce dépôt (Story 5.2).

Un serveur d'ici est un programme à part entière : il est lancé en
sous-processus, sandboxé, et parle JSON-RPC sur stdin/stdout. Il n'importe
donc rien de ``features/`` et ne touche pas la base — c'est ce qui lui permet
de vivre sous ``infra/`` et d'être atteignable par tous les appelants sans
import croisé (``.import-linter`` Contract 1).

Pourquoi un serveur interne plutôt qu'un serveur tiers : l'image backend est
``python:3.14-slim`` + ``bubblewrap`` + ``curl`` — ni Node, ni ``npx``, ni
``ripgrep``. Le dossier complet est dans
``docs/decisions/dev-pole-code-search-server.md``.
"""
