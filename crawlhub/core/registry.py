"""Platform registry and BasePlatformService.

Provides plugin.yaml-based auto-discovery and BasePlatformService ABC.
New platforms are discovered by scanning ``crawlhub/crawlers/`` for
``plugin.yaml`` files; no decorator registration is needed.
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crawlhub.core.plugin_manifest import ActionDef, PluginManifest

from crawlhub.core.task_context import TaskContext

logger = logging.getLogger(__name__)

# Global platform registry: platform_name -> service class
_PLATFORM_REGISTRY: dict[str, type[BasePlatformService]] = {}
# Per-platform manifest (injected at instantiation time)
_PLATFORM_MANIFESTS: dict[str, PluginManifest] = {}


@dataclass
class CookieStatus:
    """Cookie health status for a platform."""

    status: str  # "valid" | "expired" | "missing"
    message: str = ""
    last_checked: float | None = None


class BasePlatformService(ABC):
    """Base class for all platform crawlers.

    Subclasses must implement ``execute()``.  All other methods have
    manifest-backed defaults:

    * ``platform_name``  - from ``manifest.name``
    * ``list_actions``    - from ``manifest.actions`` keys
    * ``get_action_schema`` / ``get_action_output_schema`` - from
      ``manifest.actions[action].input_schema / .output_schema``
    * ``check_cookie``    - from ``manifest.cookie``
    """

    # BBA 登录时是否跳过 stealth 注入 + 使用最小化启动参数。
    # 快手 websig4 等 SDK 会检测 --disable-extensions 等自动化指纹，
    # 导致 QR 登录失败，需要 skip。其他平台默认保留完整 stealth。
    bba_skip_stealth: bool = False

    def __init__(self, manifest: PluginManifest | None = None) -> None:
        self.manifest = manifest

    # ------------------------------------------------------------------
    # Default implementations backed by PluginManifest
    # Subclasses may override any of these for custom behaviour.
    # ------------------------------------------------------------------

    def platform_name(self) -> str:
        """Return platform unique identifier.

        The default reads ``self.manifest.name``.  Subclasses that do
        not use a manifest may override this method.
        """
        if self.manifest is not None:
            return self.manifest.name
        raise NotImplementedError(
            "platform_name() must be implemented or a manifest must be provided"
        )

    def list_actions(self) -> list[str]:
        """Return list of supported action names.

        Default reads ``self.manifest.actions`` keys.
        """
        if self.manifest is not None:
            return list(self.manifest.actions.keys())
        raise NotImplementedError(
            "list_actions() must be implemented or a manifest must be provided"
        )

    def get_action_schema(self, action: str) -> dict[str, Any]:
        """Return JSON Schema for *action*'s input parameters.

        Default reads ``self.manifest.actions[action].input_schema``.
        """
        if self.manifest is not None:
            if action not in self.manifest.actions:
                raise KeyError(f"Action '{action}' not declared in manifest")
            return self.manifest.actions[action].input_schema
        raise NotImplementedError(
            "get_action_schema() must be implemented or a manifest must be provided"
        )

    @abstractmethod
    def execute(self, action: str, params: dict[str, Any], ctx: TaskContext) -> None:
        """Execute the crawl action within the given TaskContext.

        Use ctx.write_record(obj), ctx.set_progress(x), ctx.log(msg)
        to interact with the scheduler.
        """

    def check_cookie(self) -> CookieStatus:
        """Check current cookie health status.

        Default reads ``self.manifest.cookie``.  Returns
        ``CookieStatus(status="valid")`` when ``cookie.required`` is
        False, or ``CookieStatus(status="missing")`` otherwise.
        Subclasses should override with real cookie validation.
        """
        if self.manifest is not None:
            cookie_cfg = self.manifest.cookie
            if not getattr(cookie_cfg, "required", False):
                return CookieStatus(status="valid")
            return CookieStatus(status="missing", message="Cookie required but not configured")
        # Fallback: assume valid (legacy behaviour)
        return CookieStatus(status="valid")

    def get_action_output_schema(self, action: str) -> dict[str, str] | None:
        """Return the output schema for *action*'s produced records.

        Reads exclusively from ``self.manifest.actions[action].output_schema``
        (declared in plugin.yaml). Returns ``None`` if the action has no
        manifest entry or did not declare an ``output_schema``.

        Types should use DuckDB names: INTEGER, VARCHAR, DOUBLE, BOOLEAN,
        JSON, BIGINT, TIMESTAMP.

        Returns the v1-compatible ``{field: type_str}`` form. For the v2
        form (with label/description), use
        ``self.manifest.actions[action].get_output_schema_v2()`` directly.
        """
        if self.manifest is None:
            return None
        action_def = self.manifest.actions.get(action)
        if action_def is None:
            return None
        # Empty dict -> not declared.
        if not action_def.output_schema:
            return None
        return action_def.get_output_schema()


# ---------------------------------------------------------------------------
# Registry query helpers (public API — signatures preserved)
# ---------------------------------------------------------------------------

def get_registry() -> dict[str, type[BasePlatformService]]:
    """Return the current platform registry (read-only view)."""
    return dict(_PLATFORM_REGISTRY)


def get_platform_service(name: str) -> type[BasePlatformService] | None:
    """Get a registered platform service class by name."""
    return _PLATFORM_REGISTRY.get(name)


def get_output_schema(platform: str, action: str) -> dict[str, str] | None:
    """Top-level helper: return the declared output schema for platform.action.

    Reads from the platform's ``PluginManifest`` (loaded by
    ``discover_platforms()`` and stored in ``_PLATFORM_MANIFESTS``). No
    instantiation, no side effects. Returns ``None`` if the platform isn't
    registered, or the action has no ``output_schema`` declared in
    ``plugin.yaml``.

    Returns the v1-compatible ``{field: type_str}`` form. For the v2 form
    (with label/description) use
    ``get_platform_manifest(platform).actions[action].get_output_schema_v2()``.

    Used by the SQL items_from L2 validator and the
    GET /api/actions/<p>/<a>/schema endpoint.
    """
    manifest = _PLATFORM_MANIFESTS.get(platform)
    if manifest is None:
        return None
    action_def = manifest.actions.get(action)
    if action_def is None:
        return None
    if not action_def.output_schema:
        return None
    return action_def.get_output_schema()


# ---------------------------------------------------------------------------
# Plugin auto-discovery (task 1.2 / 1.3)
# ---------------------------------------------------------------------------

def discover_platforms() -> None:
    """Scan ``crawlhub/crawlers/`` for ``plugin.yaml`` manifests and
    register all discovered platform service classes.

    This should be called once at Daemon startup.  Each manifest's
    ``entry`` field (``module.path:ClassName``) is imported and the
    class is registered into ``_PLATFORM_REGISTRY``.
    """
    # Default crawlers root: crawlhub/crawlers/
    try:
        import crawlhub.crawlers as _crawlers_pkg
        crawlers_root = Path(_crawlers_pkg.__file__).parent
    except (ImportError, AttributeError):
        # Fallback: relative to this file -> ../../crawlers/
        crawlers_root = Path(__file__).resolve().parent.parent / "crawlers"

    logger.info("[INFO] discover_platforms: scanning %s", crawlers_root)
    manifests = _scan_crawlers_dir(crawlers_root)

    from crawlhub.core.shape_validator import validate_crawler_shape

    for manifest in manifests:
        # ---- R7: dataclass-vs-yaml field equality check ------------------
        # When an action declares ``output_dataclass: module:Class`` in
        # plugin.yaml, the dataclass field set MUST equal the yaml
        # output_schema keys *exactly*. Any drift (extra/missing/renamed
        # field) rejects the platform at startup so we never ship a
        # crawler that silently writes the wrong shape.
        #
        # Synthetic keys that crawlhub itself injects after the dataclass
        # round-trip (e.g. ``_source_video``, ``_source_post``,
        # ``_source_uid``) are tolerated on the yaml side because they are
        # added by service/bridge code, not by the dataclass. They must
        # still be declared in yaml so DuckDB knows the column type.
        r7_violations = _check_output_dataclass_contract(manifest)
        if r7_violations:
            for v in r7_violations:
                logger.error("[ERR] crawler '%s' R7 violation: %s", manifest.name, v)
            logger.error(
                "[ERR] platform '%s' rejected: dataclass/yaml output mismatch (R7)",
                manifest.name,
            )
            continue

        # ---- Static shape check (R1 + R2 + R3-deep-import) ---------------
        # Phase-1 (migration window): WARN only so legacy platforms keep
        # running while sub-agents migrate them. Phase-2 (post-migration)
        # will flip this to fail-fast — see CRWL-002 task 11.
        platform_dir = crawlers_root / manifest.name
        # In tests, the directory name may not match manifest.name when
        # we feed an arbitrary _scan_crawlers_dir root.  Fall back to
        # walking the manifest's source location if needed.
        if not platform_dir.is_dir():
            # Best-effort: search for plugin.yaml whose name matches.
            for entry in crawlers_root.iterdir():
                if entry.is_dir() and (entry / "plugin.yaml").is_file():
                    try:
                        from crawlhub.core.plugin_manifest import load_manifest as _lm
                        m = _lm(entry / "plugin.yaml")
                        if m.name == manifest.name:
                            platform_dir = entry
                            break
                    except Exception:  # noqa: BLE001
                        continue
        shape_report = validate_crawler_shape(platform_dir, manifest)
        if not shape_report.ok:
            for v in shape_report.violations:
                logger.warning(
                    "[WARN] crawler '%s' shape violation: %s",
                    manifest.name, v,
                )

        module_path = manifest.module_path()
        class_name = manifest.class_name()
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            logger.error(
                "[ERR] Failed to import module '%s' for platform '%s': %s",
                module_path,
                manifest.name,
                exc,
            )
            continue

        svc_cls = getattr(module, class_name, None)
        if svc_cls is None:
            logger.error(
                "[ERR] Class '%s' not found in module '%s' for platform '%s'",
                class_name,
                module_path,
                manifest.name,
            )
            continue

        # Register: use name from manifest (ground truth)
        if manifest.name in _PLATFORM_REGISTRY:
            logger.warning(
                "[WARN] Platform '%s' already registered, overwriting with %s",
                manifest.name,
                svc_cls.__name__,
            )
        _PLATFORM_REGISTRY[manifest.name] = svc_cls
        _PLATFORM_MANIFESTS[manifest.name] = manifest
        logger.info("[OK] Registered platform '%s' -> %s", manifest.name, svc_cls.__name__)

    logger.info("[INFO] discover_platforms: %d platform(s) registered", len(_PLATFORM_REGISTRY))
    return dict(_PLATFORM_REGISTRY)


def create_platform_service(name: str) -> BasePlatformService | None:
    """Instantiate a registered platform service with its manifest injected.

    Returns ``None`` if *name* is not registered.
    """
    cls = _PLATFORM_REGISTRY.get(name)
    if cls is None:
        return None
    manifest = _PLATFORM_MANIFESTS.get(name)
    return cls(manifest=manifest)


def get_platform_manifest(name: str) -> PluginManifest | None:
    """Return the loaded ``PluginManifest`` for *name*, or ``None`` if the
    platform isn't registered.

    Read-only accessor for the private ``_PLATFORM_MANIFESTS`` dict. Used
    by API routes (e.g. ``/api/platforms``) that need to surface
    user-facing fields like ``display_name`` and ``description`` without
    instantiating a service.
    """
    return _PLATFORM_MANIFESTS.get(name)


def get_action_meta(platform: str, action: str) -> ActionDef | None:
    """Return manifest-backed action metadata without instantiating a service."""
    manifest = _PLATFORM_MANIFESTS.get(platform)
    if manifest is None:
        return None
    return manifest.actions.get(action)





def _scan_crawlers_dir(root: Path) -> list[PluginManifest]:
    """Scan *root* for crawler plugin directories containing ``plugin.yaml``.

    Discovery rules
    -----------------
    * Only **direct sub-directories** of *root* are considered.
    * Directories whose name starts with ``_`` (e.g. ``_template``) are
      **silently skipped** — they are reserved for scaffolding / helpers.
    * ``__pycache__`` is also skipped.
    * A directory **without** a ``plugin.yaml`` file is skipped (no error).
    * A directory **with** an invalid ``plugin.yaml`` logs an ``ERROR``
      and is skipped — other directories are **not** affected (TC-D03).
    * Two manifests declaring the **same ``name``** is a duplicate error;
      the second one is **rejected** (TC-D04).

    Parameters
    ----------
    root : Path
        Typically ``Path(crawlhub.crawlers.__file__).parent`` or a tmp
        path in tests.

    Returns
    -------
    list[PluginManifest]
        Successfully loaded manifests (order is filesystem iteration order,
        which is implementation-defined — callers should not rely on it).
    """
    from crawlhub.core.plugin_manifest import load_manifest, ManifestError

    root = Path(root)
    if not root.is_dir():
        logger.warning("[WARN] crawlers root %s is not a directory, skipping", root)
        return []

    found: list[PluginManifest] = []
    seen_names: dict[str, str] = {}  # name -> directory name (for dup detection)

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name == "__pycache__":
            logger.debug("[INFO] Skipping reserved directory: %s", entry.name)
            continue

        manifest_path = entry / "plugin.yaml"
        if not manifest_path.is_file():
            logger.debug("[INFO] No plugin.yaml in %s, skipping", entry.name)
            continue

        # --- Load & validate ------------------------------------------------
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            logger.error(
                "[ERR] Failed to load plugin manifest %s: %s",
                manifest_path,
                exc,
            )
            continue  # TC-D03: don't abort the whole scan
        except Exception as exc:
            logger.error(
                "[ERR] Unexpected error loading %s: %s",
                manifest_path,
                exc,
            )
            continue

        # --- Duplicate name check (TC-D04) ---------------------------------
        if manifest.name in seen_names:
            logger.error(
                "[ERR] Duplicate platform name '%s' in %s (already declared by %s), skipping",
                manifest.name,
                entry.name,
                seen_names[manifest.name],
            )
            continue

        seen_names[manifest.name] = entry.name
        found.append(manifest)
        logger.info("[OK] Discovered crawler: %s (from %s)", manifest.name, entry.name)

    return found


# ---------------------------------------------------------------------------
# R7: dataclass <-> yaml output_schema field-equality check
# ---------------------------------------------------------------------------

# Synthetic columns that crawlhub itself injects into a record after the
# dataclass has produced its dict. They are declared in yaml.output_schema
# (so DuckDB knows their type) but MUST NOT exist on the dataclass — they
# come from service / bridge code, not from the crawler payload.
#
# Keep this list explicit and small; if a new synthetic key shows up, add
# it here AND make sure the producer is in service.py / bridge.py.
_SYNTHETIC_RECORD_KEYS: frozenset[str] = frozenset({
    "_source_video",  # bilibili / kuaishou / douyin scrape_comments
    "_source_post",   # weibo scrape_comments
})


def _check_output_dataclass_contract(manifest: PluginManifest) -> list[str]:
    """Validate that every action's ``output_dataclass`` matches its
    ``output_schema`` field-for-field.

    Returns a list of human-readable violation strings (empty = pass).
    Actions without ``output_dataclass`` declared are skipped (legacy
    platforms still run; only the layer-3 runtime warn applies).
    """
    violations: list[str] = []
    for action_name, action_def in manifest.actions.items():
        oc_ref = action_def.output_dataclass
        if not oc_ref:
            continue  # opt-in: legacy actions skip R7

        # Resolve "module.path:ClassName" -> dataclass type
        try:
            module_path, class_name = oc_ref.split(":", 1)
        except ValueError:
            violations.append(
                f"action='{action_name}' output_dataclass={oc_ref!r} is not "
                f"'module.path:ClassName'"
            )
            continue

        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001
            violations.append(
                f"action='{action_name}' cannot import module '{module_path}' "
                f"for output_dataclass: {exc}"
            )
            continue

        cls = getattr(module, class_name, None)
        if cls is None:
            violations.append(
                f"action='{action_name}' class '{class_name}' not found in "
                f"module '{module_path}'"
            )
            continue

        # Use dataclasses.fields() so we don't have to instantiate the type.
        try:
            from dataclasses import fields as _dc_fields, is_dataclass
        except ImportError:  # extremely unlikely
            violations.append(f"action='{action_name}' dataclasses unavailable")
            continue

        if not is_dataclass(cls):
            violations.append(
                f"action='{action_name}' output_dataclass {oc_ref} is not a "
                f"@dataclass"
            )
            continue

        dc_fields = {f.name for f in _dc_fields(cls)}
        yaml_fields = set(action_def.output_schema.keys()) - _SYNTHETIC_RECORD_KEYS

        extra_in_dc = dc_fields - yaml_fields
        missing_in_dc = yaml_fields - dc_fields

        if extra_in_dc or missing_in_dc:
            parts = [f"action='{action_name}' dataclass={oc_ref}"]
            if extra_in_dc:
                parts.append(
                    f"fields on dataclass not in yaml output_schema: "
                    f"{sorted(extra_in_dc)}"
                )
            if missing_in_dc:
                parts.append(
                    f"yaml output_schema keys not on dataclass: "
                    f"{sorted(missing_in_dc)}"
                )
            violations.append("; ".join(parts))

    return violations

