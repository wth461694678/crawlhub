"""Implementation details for the `{{platform_name}}` crawler.

R3 — Modules under `_internal/` are PRIVATE to this platform.
`service.py` and any sibling `bridge.py` MUST NOT import from here
(enforced by C4 / C5 in `tests/test_platform_conformance.py`).

Acceptable contents:
    * cookie / session managers
    * signing helpers (e.g. abogus, gxxxx)
    * browser-profile builders
    * rate-limit helpers
    * platform-specific parsers reused across actions

If something here needs to be visible to `service.py`, re-export it via
`crawler/__init__.py` so the dependency is explicit.
"""
