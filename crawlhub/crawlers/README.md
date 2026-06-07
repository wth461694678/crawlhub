# crawlhub/crawlers/ — Platform Plugin Registry

This directory is the **discovery root** for all crawler plugins.
Each direct subdirectory is one independent platform crawler that is
auto-discovered by `crawlhub.core.registry.discover_platforms()` at
daemon startup — no manual registration, no decorators, no imports
from `daemon.py`.

> **Adding a new platform?** Don't copy any directory by hand. Run
>
> ```bash
> crawlhub platform new <your_platform_name>
> ```
>
> This generates a complete, conformance-passing scaffold under
> `crawlhub/crawlers/<your_platform_name>/`, including a `README.md`
> tailored to your platform. See `crawlhub/scaffolding/` for the
> generator source.

---

## How the platform layer and crawler layer are decoupled

```
┌─────────────────── PLATFORM LAYER (crawlhub/core/, immutable) ────────────────────┐
│                                                                                    │
│  registry.discover_platforms()                                                     │
│       │ 1. scan crawlhub/crawlers/*/plugin.yaml                                    │
│       │ 2. load_manifest()       — validate yaml (R4)                              │
│       │ 3. R7 contract check     — dataclass.fields == output_schema.keys         │
│       │ 4. validate_crawler_shape — R1/R2/R3/R6 static checks                     │
│       │ 5. importlib.import_module(manifest.entry)                                 │
│       │    -> register into _PLATFORM_REGISTRY                                     │
│       ▼                                                                            │
│  daemon: create_platform_service(name).execute(action, params, ctx)                │
│                                                       ↑                            │
│                                                       │ ONE contract               │
│                                                       │ BasePlatformService.execute│
└───────────────────────────────────────────────────────┼────────────────────────────┘
                                                        │
┌───────────────────────────────────────────────────────┼────────────────────────────┐
│  CRAWLER LAYER (crawlhub/crawlers/<x>/, owned by plugin author)                    │
│                                                                                    │
│  service.py    -> dispatch (action name == scraper method name)                    │
│  crawler/__init__.py   -> sole public entry: from .scraper import <X>Scraper       │
│  crawler/scraper.py    -> orchestration (pagination / merge / yield)               │
│  crawler/client.py     -> network (HTTP, signing, cookies)                         │
│  crawler/models.py     -> @dataclass + .to_dict() (== plugin.yaml output_schema)   │
│  crawler/_internal/    -> platform-private helpers (R3: NOT imported by service.py)│
└────────────────────────────────────────────────────────────────────────────────────┘
```

The platform layer never knows which crawlers exist; crawlers never know
the daemon, batch runner, or scheduler exist. The four contact points
are:

| Contact   | Protocol                                                            | Enforced by                                      |
|-----------|---------------------------------------------------------------------|--------------------------------------------------|
| Discovery | Direct subdirectory of `crawlers/` containing `plugin.yaml`          | `registry._scan_crawlers_dir`                    |
| Entry     | `entry: "module:Class"` resolves to a `BasePlatformService` subclass | `registry.discover_platforms`                    |
| Behavior  | `execute(action, params, ctx)` — one ABC method                      | `BasePlatformService` (abstract)                 |
| Data      | `plugin.yaml` `output_schema` == dataclass fields == `to_dict()` keys| R7 check + `tests/test_platform_conformance.py` C3 |

---

## Conformance rules (C1–C10)

Every platform under this directory is validated by
[`tests/test_platform_conformance.py`](../../tests/test_platform_conformance.py).
A platform that fails any check at the **ERR** level will be rejected by
the daemon on startup.

| ID  | Check                                                                      | Level    |
|-----|----------------------------------------------------------------------------|----------|
| C1  | `plugin.yaml` exists and parses                                            | ERR      |
| C2  | Every action declares an importable `output_dataclass`                     | ERR      |
| C3  | `dataclass.fields()` == `output_schema.keys()` (minus synthetic columns)   | ERR      |
| C4  | `service.py` does NOT import `crawler._internal.*`                         | ERR      |
| C5  | `bridge.py` does NOT import `crawler._internal.*`                          | ERR      |
| C6  | No `output/` / `data/` / `logs/` / `cache/` / `downloads/` subdirectories  | ERR      |
| C7  | No `ctx.write_record(asdict(...))` or mixed inline-dict writes             | ERR/WARN |
| C8  | Every `output_dataclass` exposes a callable `to_dict()`                    | ERR      |
| C9  | `service.py` / `bridge.py` does not write to package-relative paths        | WARN     |
| C10 | `service.execute()` dispatches every action declared in `plugin.yaml`      | WARN     |
| C11 | `entry` is a fully-qualified import path resolving to a `BasePlatformService` | ERR    |

And the shape rules (R1–R7) enforced by `core/shape_validator.py` and
`core/registry.py`:

| ID | Rule                                                                                  |
|----|---------------------------------------------------------------------------------------|
| R1 | Required files exist: `__init__.py`, `service.py`, `plugin.yaml`, `crawler/{__init__,scraper,client,models}.py` |
| R2 | `crawler/__init__.py` re-exports `<PascalCase(name)>Scraper`                          |
| R3 | Three-layer separation: service / scraper / client / models / _internal               |
| R4 | Each yaml action declares non-empty `description` / `input_schema` / `output_schema` (schema v2: every output column MUST have a non-empty `label`; legacy bare-string form is rejected) |
| R6 | No platform-local output directories (use `ctx.output_dir` instead)                   |
| R7 | dataclass fields == yaml `output_schema` keys (modulo synthetic `_source_*`)          |

---

## Tooling

| Command                                                                       | Use                                            |
|-------------------------------------------------------------------------------|------------------------------------------------|
| `crawlhub platform new <name>`                                                | Scaffold a new platform                        |
| `crawlhub platform list`                                                      | List all registered platforms (daemon must run)|
| `python tests/test_platform_conformance.py`                                   | Static C1–C10 check (no daemon needed)         |
| `python tests/test_platform_conformance.py --platform <name>`                 | Static check for one platform                  |
| `python -m crawlhub.scripts.verify_crawlers --only <name>`                    | R1/R2/R3-static/R4 check for one platform      |
| `pytest tests/test_platform_conformance.py`                                   | pytest-parametrized version (CI gate)          |

---

## Reserved names

The discovery scanner silently skips any direct child whose name starts
with `_` (e.g. `_template`, `_archive`) and `__pycache__`. Don't use
those prefixes for real platforms.
