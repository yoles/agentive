"""Feature modules — M1 to M12 (isolated from each other).

Empty in Sprint 0. Each module will be implemented in its respective epic :

    - company_architect : Epic 11 (Growth)
    - agent_registry    : Epic 2
    - workflow_engine   : Epic 4
    - memory_manager    : Epic 3
    - tool_hub          : Epic 2 (partie)
    - dashboard         : Epic 7
    - chat              : Epic 6
    - agent_configurator: Epic 2 (partie)
    - topology          : Epic 13 (Growth)
    - reporting        : Epic 13 (Growth)
    - scheduler        : Epic 7
    - trace            : Epic 8

Each feature has this structure :
    features/mN_name/
    ├── __init__.py    # Barrel — PUBLIC API
    ├── service.py
    ├── schemas.py
    ├── events.py
    └── tests/

Inter-feature communication : bus d'événements uniquement (import forbidden).
"""

from __future__ import annotations
