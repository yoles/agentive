"""Application bootstrap — FastAPI entry point.

Submodules :
    - main : FastAPI app factory + serve() CLI entry
    - lifespan : startup/shutdown hooks
    - middleware : correlation_id, auth token, CORS
"""

from __future__ import annotations
