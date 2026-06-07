"""FastAPI application factory for CrawlHub Daemon.

Creates the FastAPI app with all routes, middleware, and static file serving.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from crawlhub._version import __version__ as _CRAWLHUB_VERSION


if TYPE_CHECKING:
    from crawlhub.core.daemon import CrawlHubDaemon


def create_app(daemon: CrawlHubDaemon) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="CrawlHub Daemon",
        version=_CRAWLHUB_VERSION,
        description="Unified Crawler Platform API",
    )

    # Store daemon reference for dependency injection
    app.state.daemon = daemon

    # CORS (permissive for local dev, same-origin in production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API routes
    from crawlhub.api.routes import router as api_router

    app.include_router(api_router)

    # Health endpoint at root level
    @app.get("/health")
    def health_check():
        # `total_record_count` is the system-wide lifetime download counter
        # (monotonic; survives retry/archive/purge of any task). O(1) read
        # of an in-memory atomic counter — never queries SQLite. See
        # crawlhub/core/flux.py for the design.
        try:
            total_record_count = daemon.flux.snapshot()
        except Exception:
            total_record_count = 0
        return {
            "status": "ok",
            "uptime": round(daemon.uptime, 1),
            "db_ok": True,
            "running_tasks": daemon.store.count_by_status("running"),
            "queued_tasks": daemon.store.count_by_status("queued"),
            "running_top_tasks": daemon.store.count_top_tasks_by_status("running"),
            "queued_top_tasks": daemon.store.count_top_tasks_by_status("queued"),
            "total_record_count": total_record_count,
            "memory_mb": _get_memory_mb(),
            "version": _CRAWLHUB_VERSION,
        }

    # Shutdown hook
    @app.on_event("shutdown")
    async def shutdown_event():
        daemon.graceful_shutdown()

    # Try to mount frontend static files (if built)
    _mount_frontend(app)

    return app


def _get_memory_mb() -> float:
    """Get current process memory usage in MB."""
    try:
        import psutil
        process = psutil.Process()
        return round(process.memory_info().rss / (1024 * 1024), 1)
    except ImportError:
        # psutil not available, use basic approach
        import os
        import sys
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return round(pmc.WorkingSetSize / (1024 * 1024), 1)
        return 0.0


def _mount_frontend(app: FastAPI) -> None:
    """Mount frontend static files."""
    from pathlib import Path

    # Look for frontend in the package directory
    possible_paths = [
        Path(__file__).parent.parent / "frontend",
    ]

    for dist_path in possible_paths:
        if dist_path.exists() and (dist_path / "index.html").exists():
            app.mount("/", StaticFiles(directory=str(dist_path), html=True), name="frontend")
            break
