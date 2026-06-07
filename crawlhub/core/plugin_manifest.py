"""Plugin manifest data class and schema validator.

Defines the ``PluginManifest`` dataclass that mirrors ``plugin.yaml`` and
``load_manifest(path)`` which validates + deserialises a single manifest
file.

Test-contract: TC-M01 .. TC-M05 in ``tests/test_plugin_manifest.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import yaml


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class ManifestError(ValueError):
    """Raised when a plugin.yaml fails validation.

    Attributes
    ----------
    field : str | None
        The offending YAML key, if the error is field-specific.
    path  : Path | None
        The manifest file that caused the error.
    """

    def __init__(self, message: str = "", *, field: str | None = None, path: Path | None = None):
        super().__init__(message)
        self.field = field
        self.path = path


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class FieldDef:
    """Schema-v2 metadata for a single output_schema field.

    Three attributes only — keep it minimal so plugin authors aren't tempted
    to hide real semantics behind ad-hoc keys (units / notes / etc. all go
    into ``description``).

    Attributes
    ----------
    type : str
        DuckDB type name (VARCHAR / int / bool / JSON / float / ...). REQUIRED.
    label : str
        Human-readable Chinese label for UI display. Empty string when the
        plugin author hasn't migrated yet (v1 legacy form).
    description : str
        Optional semantic explanation, including unit / range / enum mapping.
        Shown as tooltip on hover in drawer & action-schema modal.
    """

    type: str = ""
    label: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        """Serialise to a plain dict (for JSON / API responses)."""
        return {"type": self.type, "label": self.label, "description": self.description}


@dataclass
class BrowserConfig:
    """Browser-backed action runtime limits from plugin.yaml.

    R7 极简化（spec R7-browser-page-action-lifecycle.md §7.1）：
      - 删除 R5 的 max_session_concurrency / page_pool_size / page_operation_concurrency
        / session_idle_ttl_seconds / session_max_age_seconds / lease_policy 6 个字段
      - chrome 由 SessionKey singleflight 自动复用，无需 max_session_concurrency 配置
      - page 由 action 通过 with hold() 按需 lazy 创建，无上限
      - 关闭机制完全由 hold lifecycle + daemon 兜底接管，无需 idle_ttl/max_age
      - lease_policy 二分模型废除，所有 BBA action 走统一 hold/page 路径
      - 仅保留 session_scope 作为未来扩展（browser_profile_id）的占位
    """

    session_scope: str = "platform_cookie"

    def to_dict(self) -> dict[str, str | int]:
        """Serialise to a plain dict for API/debug consumers."""
        return {
            "session_scope": self.session_scope,
        }


@dataclass
class ActionDef:

    """Metadata for a single action declared in plugin.yaml.

    Field names mirror the YAML keys exactly:
      - ``display_name``    : short UI label for the action, maintained in plugin.yaml
      - ``input_schema``    : JSON-Schema-ish dict describing action parameters
      - ``output_schema``   : dict[str, FieldDef] describing produced records.

        ``FieldDef`` carries ``type`` (DuckDB type), ``label`` (Chinese name)
        and ``description``. Plugin authors may write either:
            ``app_id: VARCHAR``                        # v1 legacy
            ``app_id: {type: VARCHAR, label: "App ID"}``  # v2 recommended
        The v1 form is auto-coerced to ``FieldDef(type="VARCHAR", label="", description="")``.
      - ``output_dataclass``: optional ``"module.path:ClassName"`` reference to the
        dataclass whose fields MUST exactly equal ``output_schema`` keys.
        When set, registry runs a strict equality check at startup (R7); a
        mismatch rejects the platform. ``None`` keeps legacy behaviour
        (yaml-only contract, layer-3 runtime warn still applies).
    """

    display_name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, FieldDef] = field(default_factory=dict)
    output_dataclass: str | None = None
    runtime: str = "stateless"
    throttle_scope: str = "task"
    transport: str = "http"
    browser: BrowserConfig = field(default_factory=BrowserConfig)


    def __post_init__(self) -> None:
        """Auto-coerce runtime and output schema forms.

        Plugin-yaml loading normalises values before construction. Programmatic
        construction used by tests/services can still pass legacy output schema
        forms or a raw browser dict, so keep the compatibility shim here.
        """
        self.transport = _coerce_transport("<programmatic>", self.transport)
        self.runtime, self.throttle_scope = _coerce_action_runtime(
            "<programmatic>",
            self.runtime,
            self.throttle_scope,
            self.transport,
        )
        self.browser = _coerce_browser_config("<programmatic>", self.browser)
        if not self.output_schema:
            return

        # Already coerced (all values are FieldDef) -> no-op
        if all(isinstance(v, FieldDef) for v in self.output_schema.values()):
            return
        coerced: dict[str, FieldDef] = {}
        for fname, fbody in self.output_schema.items():
            if isinstance(fbody, FieldDef):
                coerced[fname] = fbody
            elif isinstance(fbody, str):
                coerced[fname] = FieldDef(type=fbody, label="", description="")
            elif isinstance(fbody, dict):
                coerced[fname] = FieldDef(
                    type=str(fbody.get("type", "")),
                    label=str(fbody.get("label", "")),
                    description=str(fbody.get("description", "")),
                )
            else:
                # Fallback: stringify so we don't crash; v2 yaml validator
                # rejects this case earlier with a clearer error.
                coerced[fname] = FieldDef(type=str(fbody), label="", description="")
        self.output_schema = coerced

    # ------------------------------------------------------------------
    # Schema accessors — v1 form preserved for legacy callers
    # ------------------------------------------------------------------

    def get_output_schema(self) -> dict[str, str]:
        """Return v1-compatible ``{field: type_str}`` mapping.

        This is the contract every existing caller expects (registry,
        DuckDB writers, SQL items_from validation, output dataclass
        equality check). Do NOT change this signature.
        """
        return {name: fd.type for name, fd in self.output_schema.items()}

    def get_output_schema_v2(self) -> dict[str, dict[str, str]]:
        """Return v2 ``{field: {type, label, description}}`` mapping.

        New API for UI / CLI / wiki generators. Plugin authors who haven't
        migrated to v2 syntax yet will see empty ``label`` / ``description``
        strings.
        """
        return {name: fd.to_dict() for name, fd in self.output_schema.items()}


@dataclass
class CookieDef:
    """Cookie requirement declaration."""

    required: bool = False
    format: str = ""


@dataclass
class PluginManifest:
    """Deserialised ``plugin.yaml``.

    Every field mirrors the YAML structure 1-to-1 so that the rest of the
    codebase can access manifest data without knowing the YAML schema.
    """

    name: str = ""
    display_name: str = ""
    version: str = ""
    description: str = ""
    entry: str = ""
    cookie: CookieDef = field(default_factory=CookieDef)
    actions: dict[str, ActionDef] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    assets: dict[str, str] = field(default_factory=dict)

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  R7 Observability — platform-level optional fields (spec §3.6)   ║
    # ╠══════════════════════════════════════════════════════════════════╣
    # ║  transport_libraries:                                             ║
    # ║    显式声明 platform 用的 transport 库（urllib3/httpx 默认含     ║
    # ║    隐式，websockets/curl_cffi 必须显式声明）。空 = 默认两件套。  ║
    # ║    plugin loader 校验未注册的库 → 加载失败，强制提醒注册新 patch。║
    # ╚══════════════════════════════════════════════════════════════════╝
    transport_libraries: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Helpers used by the discovery layer & BasePlatformService
    # ------------------------------------------------------------------

    def module_path(self) -> str:
        """Return the dotted module path part of ``entry``.

        ``"crawlhub.crawlers.bilibili.service:BilibiliService"`` →
        ``"crawlhub.crawlers.bilibili.service"``
        """
        if ":" not in self.entry:
            raise ManifestError(
                f"Invalid entry format (missing ':'): {self.entry!r}",
                field="entry",
            )
        return self.entry.split(":", 1)[0]

    def class_name(self) -> str:
        """Return the class name part of ``entry``."""
        if ":" not in self.entry:
            raise ManifestError(
                f"Invalid entry format (missing ':'): {self.entry!r}",
                field="entry",
            )
        return self.entry.split(":", 1)[1]


# ---------------------------------------------------------------------------
# YAML → dataclass helpers
# ---------------------------------------------------------------------------

_ENTRY_RE = re.compile(r"^[\w.]+:[\w]+$")


def _coerce_cookie(raw) -> CookieDef:
    if raw is None:
        return CookieDef()
    if isinstance(raw, bool):
        return CookieDef(required=raw)
    if isinstance(raw, dict):
        return CookieDef(
            required=raw.get("required", False),
            format=raw.get("format", ""),
        )
    raise ManifestError(
        f"Invalid cookie declaration: {raw!r} (expect bool or dict)",
        field="cookie",
    )


# Whitelist of keys allowed inside a single action body (plugin.yaml).
# Anything else -> ManifestError, so typos like ``schema:`` (instead of
# ``input_schema:``) fail loudly instead of being silently swallowed.
_ALLOWED_ACTION_KEYS = frozenset({
    "display_name",
    "description",
    "input_schema",
    "output_schema",
    "output_dataclass",  # optional: "module:Class" pointing at dataclass for R7
    "runtime",
    "throttle_scope",
    "transport",
    "browser",
})

_ALLOWED_RUNTIMES = frozenset({"stateless", "browser_backed"})
_ALLOWED_THROTTLE_SCOPES = frozenset({"task", "request", "none"})
_ALLOWED_TRANSPORTS = frozenset({"http", "websocket"})
_ALLOWED_BROWSER_KEYS = frozenset({
    "session_scope",
})

# R7 Observability — supported transport library names (spec §3.6 + §8)
_SUPPORTED_TRANSPORT_LIBS = frozenset({
    "urllib3", "httpx", "websockets", "curl_cffi",
})


def _coerce_transport(action_name: str, transport: Any) -> str:
    value = "http" if transport is None else transport
    if not isinstance(value, str) or value not in _ALLOWED_TRANSPORTS:
        raise ManifestError(
            f"Action '{action_name}' transport must be one of {sorted(_ALLOWED_TRANSPORTS)}, got {value!r}.",
            field=f"actions.{action_name}.transport",
        )
    return value


def _coerce_action_runtime(action_name: str, runtime: Any, throttle_scope: Any, transport: Any = "http") -> tuple[str, str]:
    if not isinstance(runtime, str) or runtime not in _ALLOWED_RUNTIMES:
        raise ManifestError(
            f"Action '{action_name}' runtime must be one of {sorted(_ALLOWED_RUNTIMES)}, got {runtime!r}.",
            field=f"actions.{action_name}.runtime",
        )
    if not isinstance(throttle_scope, str) or throttle_scope not in _ALLOWED_THROTTLE_SCOPES:
        raise ManifestError(
            f"Action '{action_name}' throttle_scope must be one of {sorted(_ALLOWED_THROTTLE_SCOPES)}, got {throttle_scope!r}.",
            field=f"actions.{action_name}.throttle_scope",
        )
    if runtime == "browser_backed" and transport == "http" and throttle_scope != "request":
        raise ManifestError(
            f"Action '{action_name}' with runtime=browser_backed and transport=http must use throttle_scope=request.",
            field=f"actions.{action_name}.throttle_scope",
        )
    if transport == "websocket" and throttle_scope != "none":
        raise ManifestError(
            f"Action '{action_name}' with transport=websocket must use throttle_scope=none.",
            field=f"actions.{action_name}.throttle_scope",
        )
    if transport == "http" and throttle_scope == "none":
        raise ManifestError(
            f"Action '{action_name}' with transport=http cannot use throttle_scope=none.",
            field=f"actions.{action_name}.throttle_scope",
        )
    return runtime, throttle_scope


def _coerce_browser_config(action_name: str, raw: Any) -> BrowserConfig:
    if raw is None:
        return BrowserConfig()
    if isinstance(raw, BrowserConfig):
        return raw
    if not isinstance(raw, dict):
        raise ManifestError(
            f"Action '{action_name}' browser config must be a mapping, got {type(raw).__name__}.",
            field=f"actions.{action_name}.browser",
        )
    unknown = set(raw.keys()) - _ALLOWED_BROWSER_KEYS
    if unknown:
        raise ManifestError(
            f"Action '{action_name}' browser config has unknown key(s) {sorted(unknown)!r}.",
            field=f"actions.{action_name}.browser",
        )
    config = BrowserConfig(**{key: raw[key] for key in raw if key in _ALLOWED_BROWSER_KEYS})
    # R7: BrowserConfig 只剩 session_scope 字段，无需校验数值/枚举（_validate_browser_config 已删）
    return config



def _validate_action_completeness(action_name: str, body: dict) -> None:

    """Enforce R4: each action MUST declare description / input_schema / output_schema.

    Raises
    ------
    ManifestError
        With ``field=actions.<name>.<offending_key>`` for precise diagnostics.
    """
    # description: non-empty string
    desc = body.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ManifestError(
            f"Action '{action_name}' missing non-empty 'description' (R4). "
            f"Every action must declare a human-readable description.",
            field=f"actions.{action_name}.description",
        )

    # input_schema: non-empty dict
    in_schema = body.get("input_schema")
    if not isinstance(in_schema, dict) or not in_schema:
        raise ManifestError(
            f"Action '{action_name}' missing non-empty 'input_schema' (R4). "
            f"For actions that take no params, declare "
            f"{{type: object, properties: {{}}, additionalProperties: false}}.",
            field=f"actions.{action_name}.input_schema",
        )

    # output_schema: non-empty dict, no additionalProperties:true escape hatch
    out_schema = body.get("output_schema")
    if not isinstance(out_schema, dict) or not out_schema:
        raise ManifestError(
            f"Action '{action_name}' missing non-empty 'output_schema' (R4). "
            f"Required for SQL items_from upstream references and DuckDB writers.",
            field=f"actions.{action_name}.output_schema",
        )
    if out_schema.get("additionalProperties") is True:
        raise ManifestError(
            f"Action '{action_name}' output_schema contains "
            f"'additionalProperties: true' which defeats schema completeness. "
            f"Declare every column explicitly (R4).",
            field=f"actions.{action_name}.output_schema",
        )


# Whitelist of keys allowed inside a single output_schema field body (v2 form).
# Kept intentionally minimal: unit / range / note all belong inside ``description``.
_ALLOWED_FIELD_KEYS = frozenset({"type", "label", "description"})


def _coerce_output_schema(action_name: str, raw: dict) -> dict[str, FieldDef]:
    """Coerce the strict v2 (``field: {type, label, description}``) form
    into a uniform ``dict[str, FieldDef]``.

    Schema v2 is now mandatory. The legacy v1 (``field: VARCHAR``) form
    is rejected loudly so every plugin author migrates to declared
    Chinese labels — without labels the UI / drawer / action-schema
    modal fall back to raw English field names, which defeats the whole
    point of the v2 upgrade.

    Validation rules (v2):
      - field body MUST be a mapping (v1 bare-string is REJECTED)
      - ``type`` is required and must be a non-empty string
      - ``label`` is REQUIRED and must be a non-empty string (UI label)
      - ``description`` is optional, defaults to ``""``
      - unknown keys (anything outside type/label/description) raise
        ManifestError — put units / notes inside ``description``

    Returns
    -------
    dict[str, FieldDef]
        Strictly validated v2 form.
    """
    out: dict[str, FieldDef] = {}
    for fname, fbody in raw.items():
        # v1 legacy ``field: VARCHAR`` — REJECTED. Migration is mechanical:
        # ``app_id: VARCHAR`` -> ``app_id: {type: VARCHAR, label: "..."}``.
        if isinstance(fbody, str):
            raise ManifestError(
                f"Action '{action_name}' output_schema field '{fname}' uses "
                f"the deprecated v1 bare-string form ({fbody!r}). Schema v2 "
                f"is now mandatory: rewrite as a mapping with explicit "
                f"'type' and 'label', e.g.\n"
                f"    {fname}:\n"
                f"      type: {fbody}\n"
                f"      label: \"<中文标签>\"\n"
                f"      description: \"<可选说明>\"",
                field=f"actions.{action_name}.output_schema.{fname}",
            )

        # v2: ``field: {type: VARCHAR, label: "中文", description: "..."}``
        if isinstance(fbody, dict):
            unknown = set(fbody.keys()) - _ALLOWED_FIELD_KEYS
            if unknown:
                raise ManifestError(
                    f"Action '{action_name}' output_schema field '{fname}' "
                    f"has unknown key(s) {sorted(unknown)!r}. "
                    f"Allowed (v2): {sorted(_ALLOWED_FIELD_KEYS)}. "
                    f"Tip: put units / notes inside 'description'.",
                    field=f"actions.{action_name}.output_schema.{fname}",
                )
            type_val = fbody.get("type")
            if not isinstance(type_val, str) or not type_val.strip():
                raise ManifestError(
                    f"Action '{action_name}' output_schema field '{fname}' "
                    f"missing required 'type' (DuckDB type name).",
                    field=f"actions.{action_name}.output_schema.{fname}.type",
                )
            label_val = fbody.get("label", "")
            desc_val = fbody.get("description", "")
            if not isinstance(label_val, str):
                raise ManifestError(
                    f"Action '{action_name}' output_schema field '{fname}' "
                    f"label must be a string, got {type(label_val).__name__}.",
                    field=f"actions.{action_name}.output_schema.{fname}.label",
                )
            # v2 mandate: label is required and non-empty.
            if not label_val.strip():
                raise ManifestError(
                    f"Action '{action_name}' output_schema field '{fname}' "
                    f"missing required 'label' (non-empty UI display text). "
                    f"Schema v2 requires every column to declare its "
                    f"human-readable Chinese label.",
                    field=f"actions.{action_name}.output_schema.{fname}.label",
                )
            if not isinstance(desc_val, str):
                raise ManifestError(
                    f"Action '{action_name}' output_schema field '{fname}' "
                    f"description must be a string, got {type(desc_val).__name__}.",
                    field=f"actions.{action_name}.output_schema.{fname}.description",
                )
            out[fname] = FieldDef(type=type_val, label=label_val, description=desc_val)
            continue

        raise ManifestError(
            f"Action '{action_name}' output_schema field '{fname}' must be "
            f"a mapping with 'type'/'label'/'description' (schema v2), "
            f"got {type(fbody).__name__}.",
            field=f"actions.{action_name}.output_schema.{fname}",
        )
    return out


def _coerce_actions(raw) -> dict[str, ActionDef]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"actions must be a mapping, got {type(raw).__name__}", field="actions")
    out: dict[str, ActionDef] = {}
    for action_name, action_body in raw.items():
        if not isinstance(action_body, dict):
            # Bare-string description is no longer accepted under R4.
            raise ManifestError(
                f"Action '{action_name}' must be a mapping with "
                f"description/input_schema/output_schema, "
                f"got {type(action_body).__name__}",
                field=f"actions.{action_name}",
            )

        unknown = set(action_body.keys()) - _ALLOWED_ACTION_KEYS
        if unknown:
            raise ManifestError(
                f"Unknown key(s) {sorted(unknown)!r} in action '{action_name}'. "
                f"Allowed: {sorted(_ALLOWED_ACTION_KEYS)}. "
                f"(Hint: did you mean 'input_schema' instead of 'schema'?)",
                field=f"actions.{action_name}",
            )

        # R4 completeness check (raises ManifestError on any miss)
        _validate_action_completeness(action_name, action_body)

        display_name_raw = action_body.get("display_name", "")
        if display_name_raw is not None and not isinstance(display_name_raw, str):
            raise ManifestError(
                f"Action '{action_name}' display_name must be a string, "
                f"got {type(display_name_raw).__name__}.",
                field=f"actions.{action_name}.display_name",
            )

        # output_dataclass: optional. When present must be a string of the
        # form "module.path:ClassName". Strict-equality field check happens
        # in registry (R7), not here, because we don't want manifest loading
        # to import platform code.

        oc_raw = action_body.get("output_dataclass")
        if oc_raw is not None:
            if not isinstance(oc_raw, str) or ":" not in oc_raw:
                raise ManifestError(
                    f"Action '{action_name}' output_dataclass must be "
                    f"'module.path:ClassName' (got {oc_raw!r}).",
                    field=f"actions.{action_name}.output_dataclass",
                )

        transport = _coerce_transport(action_name, action_body.get("transport", "http"))
        runtime, throttle_scope = _coerce_action_runtime(
            action_name,
            action_body.get("runtime", "stateless"),
            action_body.get("throttle_scope", "task"),
            transport,
        )
        browser_config = _coerce_browser_config(action_name, action_body.get("browser"))
        # R7: lease_policy 字段已删除，无需 manual+websocket 互斥校验

        out[action_name] = ActionDef(
            display_name=display_name_raw or "",
            description=action_body["description"],
            input_schema=action_body["input_schema"],
            output_schema=_coerce_output_schema(action_name, action_body["output_schema"]),
            output_dataclass=oc_raw,
            runtime=runtime,
            throttle_scope=throttle_scope,
            transport=transport,
            browser=browser_config,
        )


    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_manifest(path: Path | str) -> PluginManifest:
    """Load and validate a ``plugin.yaml`` file.

    Parameters
    ----------
    path : Path | str
        Filesystem path to the YAML file.

    Returns
    -------
    PluginManifest
        Validated + deserialised manifest.

    Raises
    ------
    ManifestError
        - YAML syntax error (TC-M05)
        - Missing required field (TC-M02)
        - Invalid ``entry`` format (TC-M04)
    """
    path = Path(path)

    # -- TC-M05: wrap YAML errors ----------------------------------------
    try:
        raw_text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(
            f"Invalid YAML syntax in {path}: {exc}",
            field=None,
            path=path,
        ) from exc

    if not isinstance(raw, dict):
        raise ManifestError(
            f"plugin.yaml must be a mapping at top level, got {type(raw).__name__}",
            field=None,
            path=path,
        )

    # -- TC-M02: required fields -----------------------------------------
    REQUIRED = ("name", "display_name", "version", "entry")
    for field_name in REQUIRED:
        if field_name not in raw or raw.get(field_name) in (None, ""):
            raise ManifestError(
                f"Missing or empty required field: {field_name}",
                field=field_name,
                path=path,
            )

    # -- TC-M04: entry format validation --------------------------------
    entry_val = str(raw["entry"])
    if ":" not in entry_val or not _ENTRY_RE.match(entry_val):
        raise ManifestError(
            f"Invalid entry format {entry_val!r}; expected 'module.path:ClassName'",
            field="entry",
            path=path,
        )

    # -- Coerce to dataclass --------------------------------------------
    cookie_raw = raw.get("cookie")
    # Accept both ``cookie: true`` (bool) and ``cookie: {required: true, format: xxx}``
    if cookie_raw is None:
        cookie_def = CookieDef()
    elif isinstance(cookie_raw, bool):
        cookie_def = CookieDef(required=cookie_raw)
    elif isinstance(cookie_raw, dict):
        cookie_def = CookieDef(
            required=cookie_raw.get("required", False),
            format=cookie_raw.get("format", ""),
        )
    else:
        raise ManifestError(
            f"Invalid cookie declaration: {cookie_raw!r}",
            field="cookie",
            path=path,
        )

    manifest = PluginManifest(
        name=str(raw["name"]),
        display_name=str(raw["display_name"]),
        version=str(raw["version"]),
        description=str(raw.get("description", "")),
        entry=entry_val,
        cookie=cookie_def,
        actions=_coerce_actions(raw.get("actions")),
        dependencies=list(raw.get("dependencies", []) or []),
        assets=dict(raw.get("assets", {}) or {}),
        transport_libraries=_coerce_transport_libraries(raw.get("transport_libraries"), path),
    )
    return manifest


# ---------------------------------------------------------------------------
# R7 Observability — platform-level helpers
# ---------------------------------------------------------------------------

def _coerce_transport_libraries(raw: Any, path: Path) -> list[str]:
    """Validate plugin.yaml `transport_libraries` (optional, spec §3.6).

    Rules:
      - missing / None → []  (caller defaults to ["urllib3", "httpx"] implicitly)
      - must be a list of strings
      - each entry must be in _SUPPORTED_TRANSPORT_LIBS
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(
            f"transport_libraries must be a list, got {type(raw).__name__}",
            field="transport_libraries",
            path=path,
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ManifestError(
                f"transport_libraries entries must be non-empty strings, got {item!r}",
                field="transport_libraries",
                path=path,
            )
        normalised = item.strip()
        if normalised not in _SUPPORTED_TRANSPORT_LIBS:
            raise ManifestError(
                f"Unknown transport library: {normalised!r}; "
                f"supported: {sorted(_SUPPORTED_TRANSPORT_LIBS)}",
                field="transport_libraries",
                path=path,
            )
        out.append(normalised)
    return out
