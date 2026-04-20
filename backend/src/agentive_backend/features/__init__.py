"""Feature modules — M1 to M12 (isolated from each other).

Empty in Sprint 0. Each module will be implemented in its respective epic :

    - m1_company_architect : Epic 11 (Growth)
    - m2_agent_registry    : Epic 2
    - m3_workflow_engine   : Epic 4
    - m4_memory_manager    : Epic 3
    - m5_tool_hub          : Epic 2 (partie)
    - m6_dashboard         : Epic 7
    - m7_chat              : Epic 6
    - m8_agent_configurator: Epic 2 (partie)
    - m9_topology          : Epic 13 (Growth)
    - m10_reporting        : Epic 13 (Growth)
    - m11_scheduler        : Epic 7
    - m12_trace            : Epic 8

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
