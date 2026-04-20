"""Infrastructure adapters — concrete implementations for external systems.

Submodules :
    - db : SQLAlchemy session factory, ORM models
    - llm : Anthropic, OpenAI, Voyage, FastEmbed adapters (implémentation Story 1.6)
    - mcp : MCP JSON-RPC client + bubblewrap sandbox (implémentation Stories 2.5 + 2.6)
"""

from __future__ import annotations
