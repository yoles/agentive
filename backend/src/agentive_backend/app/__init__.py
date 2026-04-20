"""Application bootstrap — FastAPI entry point.

Submodules :
    - main : FastAPI app factory + serve() CLI entry
    - lifespan : startup/shutdown hooks
    - middleware : correlation_id (+ auth token skeleton, implementation Story 1.7)
"""

from __future__ import annotations
