"""Internal helpers for the Steam crawler.

R3 / R4 contract: ``service.py`` and any sibling ``bridge.py`` MUST NOT
import from this package (enforced by C4 / C5 / C16 in
``tests/test_platform_conformance.py``). Re-export anything that needs
to cross the boundary via ``crawler/__init__.py``.

Modules:
    game_detail   - Steam store HTML detail-page parser
    game_info     - Steam store HTML info-page parser
    reviews_v1    - Legacy review API parser
    reviews_v2    - Current review API parser
    search        - Search-result page parser
    topsellers/   - Topsellers protobuf API helpers
"""
