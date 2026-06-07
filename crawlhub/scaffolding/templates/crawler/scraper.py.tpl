"""{{platform_display}} scraper — orchestration layer.

R3 / Layer responsibility:
    * Compose calls to ``client.py`` (network) and ``models.py`` (data).
    * Implement business logic: pagination, deduplication, retries that
      span multiple requests, etc.
    * Build records as ``Model(...).to_dict()`` (never raw ``asdict(...)``,
      never inline dict literals — C7 / R7).
    * Write each record via ``ctx.write_record(record)``.

R4: a public ``.client`` property is exposed so
:class:`crawlhub.core.platform.base_service.BaseService.check_cookie`
can reach the client's ``probe()`` without violating C12 (service must
not call client directly — it goes scraper -> client).

This file MUST NOT:
    * Make HTTP requests directly — that's ``client.py``.
    * Read cookies / sign requests — that's ``client.py``.
    * Build dicts by hand — that's ``models.py`` + ``.to_dict()``.

────────────────────────────────────────────────────────────
  IMPORTANT: action method signature contract
────────────────────────────────────────────────────────────
Every public action method on this class MUST have signature::

    def <action_name>(self, ctx: TaskContext, params: dict) -> None

Inside the method, pull business fields out with::

    app_id      = params["app_id"]              # required key
    max_pages   = params.get("max_pages", 10)   # optional key, with default

WHY ``params.get(...)`` AND NOT ``**params``?

The CrawlHub platform injects control flags into a task's params dict
(e.g. ``treat_empty_as_success``, and likely more in the future). These
are NOT part of any action's business signature. If you unpack the dict
with ``**params`` into named keyword arguments, Python will raise::

    TypeError: <action>() got an unexpected keyword argument
               'treat_empty_as_success'

``params.get(...)`` and ``params[...]`` naturally ignore any extra keys,
which is exactly the behavior we want.
"""

from __future__ import annotations

from crawlhub.core.cookie_resolver import CookieResolverMixin, CookieNotReady
from crawlhub.core.task_context import TaskContext

from .client import {{client_class}}
from .models import PingResult


class {{scraper_class}}(CookieResolverMixin):
    """Business-level scraper for `{{platform_name}}`.

    Each public method here MUST correspond to one action declared in
    ``plugin.yaml``. The action key in yaml is matched to the method
    name by ``BaseService.execute`` via ``getattr(scraper, action)``.

    R4 base layer integration:
        * Inherits :class:`CookieResolverMixin` so the scraper can resolve
          its cookie file via ``self.resolve_cookie_path()`` (honors the
          daemon's thread-local cookie override).
        * Sets ``PLATFORM_NAME`` so the resolver knows where to look.
        * Implements ``check_cookie_valid`` — the service's
          ``_check_missing`` hook calls this to surface a friendly
          "missing cookie" status to the dashboard.
    """

    PLATFORM_NAME = "{{platform_name}}"

    def __init__(self, client: {{client_class}} | None = None) -> None:
        self._client = client or {{client_class}}()

    @property
    def client(self) -> {{client_class}}:
        """Public accessor used by ``BaseService.check_cookie`` to reach
        the client's ``probe()`` method (R4 plan §2.2)."""
        return self._client

    # --- cookie liveness check (called by Service._check_missing) -----
    def check_cookie_valid(self) -> bool:
        """Return True if a usable cookie file exists, else raise
        :class:`CookieNotReady` so the daemon can show "missing"."""
        cookie_path = self.get_cookie_path()
        if cookie_path.exists():
            return True
        raise CookieNotReady(
            "{{platform_name}}", "No cookie file found. Please login first."
        )

    # --- action: ping ---------------------------------------------------
    # Replace this with your real actions. Keep one method per yaml entry.
    # Signature MUST be `(self, ctx: TaskContext, params: dict) -> None`.
    def ping(self, ctx: TaskContext, params: dict) -> None:
        """Trivial health check action.

        Writes a single record whose shape matches ``plugin.yaml``
        ``actions.ping.output_schema`` exactly (enforced by R7).
        """
        # Example of reading an optional param via .get():
        # message_override = params.get("message")
        result = PingResult(ok=True, message=self._client.ping())
        ctx.write_record(result.to_dict())
        ctx.set_progress(1.0)
