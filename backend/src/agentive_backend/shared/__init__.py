"""Shared transverse modules — NO business logic.

Submodules :
    - config : settings (unique point d'accès env vars)
    - auth : token validation, middleware, RBAC (Growth)
    - contracts : schemas versionnés inter-features
    - event_bus : LISTEN/NOTIFY + Outbox Pattern
    - llm : multi-provider abstraction + budget + safety
    - logging : structlog config + correlation
    - memory : port PushMemoryProvider (ISP)
    - metrics : Prometheus registry
    - repositories : Repository Pattern (SEUL accès DB)
    - feature_flags : helpers runtime flags
"""
