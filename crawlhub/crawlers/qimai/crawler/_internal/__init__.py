"""Empty private namespace for the qimai crawler.

Qimai currently has no private helpers (cookie/sign/parser logic lives
inline in client.py). This package is intentionally kept empty so all 6
platforms have the same directory shape, mirroring platforms that DO
have private helpers (e.g. douyin's a_bogus signer, kuaishou's
graphql_helper).

R3 / R4 contract: ``service.py`` and any sibling ``bridge.py`` MUST NOT
import from ``_internal/*`` (enforced by C4 / C5 / C16 in
``tests/test_platform_conformance.py``). Adding modules here is allowed
at any time — when something graduates from "scratch logic in
client.py" to "deserves its own file", drop it here.
"""
