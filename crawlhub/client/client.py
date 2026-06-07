"""CrawlHub Python Client.

Provides a programmatic interface equivalent to the REST API.
All methods communicate with the Daemon via HTTP.
"""

from __future__ import annotations

from typing import Any

import httpx


class CrawlHubDaemonNotRunning(Exception):
    """Raised when the Daemon is not reachable."""

    def __init__(self):
        super().__init__(
            "CrawlHub Daemon is not running. Please start it with: crawlhub serve"
        )


class CrawlHubClient:
    """Python client for CrawlHub Daemon.

    Usage:
        from crawlhub import CrawlHubClient
        client = CrawlHubClient()
        task = client.start_task("steam", "search_games", {"keyword": "rpg"})
        status = client.get_task_status(task["task_id"])
    """

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        if base_url is None:
            # Single source of truth: ~/.crawlhub/config.yaml
            try:
                from crawlhub.core.config import get_config
                cfg = get_config()
                base_url = f"http://{cfg.host}:{cfg.port}"
            except Exception:
                base_url = "http://127.0.0.1:8787"
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Make HTTP request, raise CrawlHubDaemonNotRunning if unreachable."""
        try:
            resp = self._client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise CrawlHubDaemonNotRunning()

        if resp.status_code >= 400:
            resp.raise_for_status()
        return resp.json()

    # --- Task operations ---

    def start_task(self, platform: str, action: str, params: dict[str, Any] | None = None) -> dict:
        """Submit a new crawl task. Returns the created Task dict."""
        return self._request("POST", "/api/tasks", json={
            "platform": platform,
            "task_type": action,
            "logic_param": params or {},
        })

    def get_task_status(self, task_id: str) -> dict:
        """Get task details by ID."""
        return self._request("GET", f"/api/tasks/{task_id}")

    def list_tasks(
        self,
        platform: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List tasks with optional filters."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if platform:
            params["platform"] = platform
        if status:
            params["status"] = status
        return self._request("GET", "/api/tasks", params=params)

    def retry_task(self, task_id: str) -> dict:
        """Retry a failed/interrupted/cancelled task."""
        return self._request("POST", f"/api/tasks/{task_id}/retry", json={})

    def cancel_task(self, task_id: str) -> dict:
        """Cancel a running task."""
        return self._request("POST", f"/api/tasks/{task_id}/cancel")

    def delete_task(self, task_id: str) -> dict:
        """Soft-delete a task (move to trash)."""
        return self._request("DELETE", f"/api/tasks/{task_id}")

    def read_result(
        self,
        task_id: str,
        offset: int = 0,
        limit: int = 100,
        filter_expr: str | None = None,
    ) -> dict:
        """Read task result records with pagination."""
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if filter_expr:
            params["filter"] = filter_expr
        return self._request("GET", f"/api/tasks/{task_id}/result", params=params)

    def export_result(self, task_id: str, format: str = "csv", output_path: str | None = None) -> dict:
        """Export task results to file."""
        body: dict[str, Any] = {"format": format}
        if output_path:
            body["output_path"] = output_path
        return self._request("POST", f"/api/tasks/{task_id}/export", json=body)

    # --- Platform info ---

    def list_platforms(self) -> dict:
        """Get registered platforms and their actions."""
        return self._request("GET", "/api/platforms")

    # --- Health ---

    def health(self) -> dict:
        """Check daemon health."""
        return self._request("GET", "/health")

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
