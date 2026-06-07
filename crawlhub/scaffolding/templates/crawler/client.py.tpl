"""{{platform_display}} HTTP / network client (R4).

Inherits :class:`crawlhub.core.platform.base_client.BaseHttpClient`,
which:

* Calls ``_setup_sessions`` exactly once at construction.
* Forces ``probe(task_type)`` to be implemented (the
  ``CapabilityProbe`` Protocol mixin makes it ABC-mandatory).
* Provides a uniform ``is_logged_in`` view over the cookie_jar.

R3 / Layer responsibility:
    * Build and send HTTP requests.
    * Handle headers, cookies, signing, retries that pertain to a single
      request (use scraper.py for multi-request orchestration).
    * Return parsed JSON / soup — but NOT domain objects (that's models.py).

This file MUST NOT:
    * Iterate pages or assemble multi-request payloads — that's scraper.py.
    * Convert raw responses into dataclasses — let the scraper do that.
"""

from __future__ import annotations

# Real platforms typically:
#     import httpx  # or: import requests
# Add your real dependency to ``plugin.yaml`` ``dependencies:`` then import here.

from crawlhub.core.platform.base_client import BaseHttpClient
from crawlhub.core.platform.cookie_jar import CookieJar
from crawlhub.core.platform.probe_protocol import ProbeResult


class {{client_class}}(BaseHttpClient):
    """Stateless wrapper around `{{platform_display}}`'s HTTP endpoints.

    The template ships a fake ``ping()`` and a deterministic ``probe()``
    so the scaffolded platform passes the conformance test with zero
    edits. Replace these with real network code as you build out the
    crawler.
    """

    def __init__(
        self,
        cookie_jar: CookieJar | None = None,
        base_url: str = "https://example.com",
    ) -> None:
        self.base_url = base_url
        super().__init__(cookie_jar=cookie_jar)

    def _setup_sessions(self) -> None:
        """Allocate HTTP session(s). Replace with a real ``requests.Session``
        / ``httpx.Client`` (single or multi-session as needed)."""
        # Real shape:
        #   import httpx
        #   self._session = httpx.Client(http2=True, timeout=10.0)
        self._session: dict = {"placeholder": True}

    def probe(self, task_type: str = "default") -> ProbeResult:
        """Probe the upstream service. Replace with a real lightweight
        endpoint hit (e.g. ``/healthz`` or ``user/info``)."""
        return ProbeResult(
            ok=True,
            api="{{platform_name}}/healthz",
            latency_ms=0,
            error=None,
            extras={"task_type": task_type},
        )

    # --- Replace these with real endpoints ------------------------------
    def ping(self) -> str:
        """Pretend to ping the upstream service.

        Real implementations would do something like::

            resp = self._session.get(f"{self.base_url}/healthz", timeout=10)
            resp.raise_for_status()
            return resp.text
        """
        return "pong"
