# plugin.yaml — CrawlHub platform plugin manifest for `{{platform_name}}`.
#
# Auto-discovered by `crawlhub.core.registry.discover_platforms()`.
# Strict rules (enforced at daemon startup and by
# `tests/test_platform_conformance.py`):
#
#   * `name` / `display_name` / `version` / `entry` are required.
#   * Every action MUST declare:
#       - `description`         (non-empty string)
#       - `input_schema`        (non-empty JSON-Schema dict; for actions
#                                with no params, use the empty-object form
#                                shown below). Each property SHOULD carry
#                                a `title` (human label, JSON-Schema
#                                standard) and `description`.
#       - `output_schema`       (non-empty {column: {type, label, description}}
#                                dict — schema v2). EVERY column MUST
#                                declare a non-empty `label`. `description`
#                                is optional but recommended.
#       - `output_dataclass`    ("module.path:ClassName"; field set MUST
#                                match `output_schema` exactly — R7)
#   * Optional action runtime controls:
#       - `runtime`             stateless | browser_backed
#       - `transport`           http | websocket
#       - `throttle_scope`      task | request | none
# R7: browser.lease_policy 已删除——所有 BBA action 走统一 hold/page 模型
#   * WebSocket actions MUST expose input fields:
#       - `duration_seconds` (integer/number)
#       - `stop_when_room_closed` (boolean)
#   * `output_schema` MUST NOT use `additionalProperties: true`.
#   * The dataclass referenced by `output_dataclass` MUST expose a
#     callable `to_dict()` method.

name: {{platform_name}}
display_name: "{{platform_display}}"
version: "0.1.0"
description: "{{platform_description}}"

# Entry point: fully-qualified "<dotted.module.path>:<ClassName>".
# MUST be an absolute import path — `discover_platforms()` feeds this
# string straight into `importlib.import_module`, so a bare
# `"service:Foo"` would try to import a top-level `service` module and
# fail to register the platform silently. The class MUST subclass
# `BasePlatformService`.
entry: "crawlhub.crawlers.{{platform_name}}.service:{{service_class}}"

cookie:
  required: false
  # description: "OAuth cookie for {{platform_display}}"   # optional UI hint

# ------------------------------------------------------------------
# Actions — one entry per public method on `{{scraper_class}}`.
# Each action key MUST equal a public method name on the scraper class.
# Replace `ping` below with your real action(s); the template ships a
# trivial `ping` purely so the scaffold passes conformance out of the box.
# ------------------------------------------------------------------
actions:
  ping:
    description: "Health check; replace with your real action."
    output_dataclass: "{{models_module}}:PingResult"
    runtime: stateless
    transport: http
    throttle_scope: task
    input_schema:
      type: object
      properties: {}
      additionalProperties: false
    output_schema:
      ok:
        type: BOOLEAN
        label: "成功标记"
        description: "true 表示健康检查通过"
      message:
        type: VARCHAR
        label: "返回消息"
        description: "可读的状态文案，便于排障"

# Pip-installable dependencies (best-effort check at discovery time).
# Add your real network deps here, e.g. `requests>=2.28`.
dependencies: []

# Static assets bundled with this plugin (paths relative to this dir).
# Access in code via: `Path(__file__).parent / "assets/<filename>"`.
# Omit this key entirely if you have no assets.
# assets:
#   - assets/example_sdk.js

