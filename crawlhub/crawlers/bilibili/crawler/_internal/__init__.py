"""Internal helpers for the bilibili crawler.

R3 / R4 contract: ``service.py`` and any sibling ``bridge.py`` MUST NOT
import from this package (enforced by C4 / C5 / C16 in
``tests/test_platform_conformance.py``). Re-export anything that needs
to cross the boundary via ``crawler/__init__.py`` so the dependency is
explicit.
"""
