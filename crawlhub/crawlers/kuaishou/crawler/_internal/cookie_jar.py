"""Kuaishou cookie jar — dual-site (main / live) data layer.

R4 P12 + R5 (2026-05-25). Replaces the cookie I/O previously embedded in
``KuaishouSession`` (``_load_cookies`` / ``_save_cookies`` /
``_build_and_save_cookies`` / ``raw_cookies`` field).

R7 P5 (2026-06-02) — schema 扩展 metadata 字段，承载 BBA 第一阶段抓到
的真实 wire headers，给 cffi 第二阶段做身份对齐用：

Schema on disk::

    {
      "main": {"did": ..., "userId": ..., "passToken": ..., ...},
      "live": {"did": ..., "kuaishou.live.web_st": ..., ...},
      "metadata": {
          "origin_user_agent":        "Mozilla/5.0 (Windows NT 10.0...) Chrome/148.0.0.0 Safari/537.36",
          "origin_sec_ch_ua":         "\"Chromium\";v=\"148\", \"Not.A/Brand\";v=\"24\", \"Google Chrome\";v=\"148\"",
          "origin_sec_ch_ua_mobile":  "?0",
          "origin_sec_ch_ua_platform":"\"Windows\"",
          "origin_accept_language":   "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
          "origin_captured_at":       1717312345.678,
          "origin_source":            "captured" | "synthesized"
      }
    }

向后兼容：旧 jar 无 ``metadata`` → 内存中为空 dict，旧文件能继续读取；
首次写入 metadata 时自动落盘新字段。

Three input shapes accepted (auto-converted to native on first read):

  * native      — ``{"main": {...}, "live": {...}}``
  * storage     — ``{"cookies": [{"name": ..., "value": ...}, ...]}``
  * flat        — ``{"key1": "val1", ...}`` (treated as main-site only)

The jar keeps a live ``data`` dict in memory; ``replace_all`` /
``update_site`` mutate it; ``save()`` persists. Login state is decided
by main-site ``did`` AND (``passToken`` OR ``kuaishou.server.webday7_st``),
matching the legacy session behaviour.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Sites supported by this jar. Order matters for "default site" semantics.
SITES: tuple[str, ...] = ("main", "live")
DEFAULT_SITE: str = "main"

# ─── Origin metadata field names ────────────────────────────────────
# 集中常量化，避免散落在 jar / playwright_runtime / http_factory 多处出错。
#
# 命名约定（铁律）：每个字段都是 wire header 名 underscore 化 + "origin_"
# 前缀，绝不缩写。这样双向映射规则单一：
#   wire "user-agent"        ↔ origin_user_agent
#   wire "sec-ch-ua-platform" ↔ origin_sec_ch_ua_platform
# 破坏对称（如曾经的 origin_ua）会让 update_origin_headers 静默丢字段。
ORIGIN_FIELDS: tuple[str, ...] = (
    "origin_user_agent",
    "origin_sec_ch_ua",
    "origin_sec_ch_ua_mobile",
    "origin_sec_ch_ua_platform",
    "origin_accept_language",
)
ORIGIN_SOURCE_CAPTURED: str = "captured"      # E1：BBA 实抓
ORIGIN_SOURCE_SYNTHESIZED: str = "synthesized"  # E2：host_info 兜底合成

# ─── Profile dir binding（R7 P6, 2026-06-03）──────────────────────────
# 把"这个 cookie 文件该用哪个 user_data_dir"显式写进 metadata，
# 替代之前依赖 Path.rename 把 _new_xxx 目录改名成 cookie_<label>
# 的脆弱路径——Windows 下 Chrome 释放文件锁有时序竞争，rename 偶发
# WinError 2 →  daemon 后续打开的是空 profile → IndexedDB / SW
# cache 全丢 → kuaishou 服务端判异常 → SSO invalidation。
#
# 字段值是相对 data_root 的 POSIX 风格路径，例如：
#   "browser_profiles/kuaishou/_new_20260603_191143"
# 跨平台 / 跨 data_root 移动都不会失效；空字符串视作未绑定，
# 调用方 fallback 旧的"按 cookie_id 算路径"逻辑。
PROFILE_DIR_FIELD: str = "profile_dir"


class KuaishouCookieJar:
    """Dual-site cookie container for kuaishou web crawler.

    Satisfies the ``CookieJar`` Protocol via ``is_logged_in`` /
    ``as_string(site)`` / ``as_dict(site)`` / ``source``. Plus a write
    path: ``replace_all`` / ``update_site`` / ``save``.
    """

    def __init__(self, file_path: Path | str) -> None:
        self._path = Path(file_path)
        # In-memory data — populated lazily on first read.
        self._data: dict[str, dict[str, str]] = {"main": {}, "live": {}}
        # ── Origin metadata（R7 P5：BBA 抓到的真实身份头持久化） ──
        # 与 _data 同生命周期，由 _refresh / replace_all / save 协同维护。
        # 字段定义见模块顶部 ORIGIN_FIELDS 常量。
        self._metadata: dict[str, str | float] = {}
        self._loaded = False
        self._mtime: float = -1.0

    # ── Path / source ───────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    def source(self) -> str:
        return str(self._path)

    # ── Load / refresh ──────────────────────────────────────

    def _refresh(self) -> None:
        """Re-read the file if its mtime changed (or first call).

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
         核心契约（R7 P5 重构，2026-06-02）
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
         已 ``_loaded`` 的内存状态**优先于**磁盘缺失/损坏。
         只有磁盘有合法新版本（mtime 变化 + JSON 可解析）才覆盖内存。

         为什么这条契约重要：
           "先 ``update_*`` 后 ``save``" 是 jar 的核心工作流。如果 _refresh
           在文件还不存在时抹掉内存，就会让"先 update 后 save"出现"幽灵
           丢数据"——你以为 update 了，其实 save 之前的下一次惰性 refresh
           把内存重置成了空。

         典型修复场景（实际触发过）：
           jar = KuaishouCookieJar(new_path)         # 文件还不存在
           jar.replace_all({...})                     # 设 _data，_loaded=True
           jar.update_origin_headers({...}, source=...)
               # 旧版 _refresh 在文件不存在时抹 _data → did 丢了
           jar.save()  # 把空 _data 写盘 ← 灾难
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        """
        path = self._path

        # 取磁盘 mtime；失败用 -1.0 作 sentinel（文件不存在 / stat 失败）
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = -1.0

        # 已 loaded 且 mtime 与上次一致（含 -1.0 持平）→ 内存即真相，no-op
        if self._loaded and abs(mtime - self._mtime) < 1e-6:
            return

        # 磁盘有合法版本 → 尝试加载
        if mtime >= 0:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("KuaishouCookieJar: cannot read %s: %s", path, exc)
                if self._loaded:
                    # 内存有数据，磁盘损坏不抹它（save 时会修复）
                    self._mtime = mtime
                    return
                # 首次加载 + 磁盘损坏 → fallthrough 到空状态初始化
            else:
                self._data = self._normalise(raw)
                self._metadata = self._extract_metadata(raw)
                self._loaded = True
                self._mtime = mtime
                return

        # 磁盘缺失（mtime < 0）+ 已 loaded → 保留内存即真相
        # 这是核心契约："已加载状态不会被磁盘缺失抹掉"
        if self._loaded:
            self._mtime = -1.0
            return

        # 首次加载 + 磁盘缺失/损坏 → 初始化空 jar
        self._data = {"main": {}, "live": {}}
        self._metadata = {}
        self._loaded = True
        self._mtime = mtime if mtime >= 0 else -1.0

    @staticmethod
    def _extract_metadata(raw) -> dict[str, str | float]:
        """Extract the optional ``metadata`` block from disk payload.

        Returns empty dict on legacy files (no metadata field). Drops any
        unknown keys defensively to keep schema strict.
        """
        if not isinstance(raw, dict):
            return {}
        meta = raw.get("metadata")
        if not isinstance(meta, dict):
            return {}
        out: dict[str, str | float] = {}
        for field in ORIGIN_FIELDS:
            v = meta.get(field)
            if isinstance(v, str) and v:
                out[field] = v
        # captured_at: float epoch
        ts = meta.get("origin_captured_at")
        if isinstance(ts, (int, float)) and ts > 0:
            out["origin_captured_at"] = float(ts)
        # source: enum-ish
        src = meta.get("origin_source")
        if isinstance(src, str) and src in (
            ORIGIN_SOURCE_CAPTURED, ORIGIN_SOURCE_SYNTHESIZED,
        ):
            out["origin_source"] = src
        # profile_dir: 相对 data_root 的路径字符串
        pd = meta.get(PROFILE_DIR_FIELD)
        if isinstance(pd, str) and pd:
            out[PROFILE_DIR_FIELD] = pd
        return out

    @staticmethod
    def _normalise(raw) -> dict[str, dict[str, str]]:
        """Coerce any of the 3 supported shapes into ``{main, live}``."""
        if isinstance(raw, dict) and "main" in raw and isinstance(raw["main"], dict):
            # Native
            return {
                "main": {str(k): str(v) for k, v in raw.get("main", {}).items()},
                "live": {str(k): str(v) for k, v in raw.get("live", {}).items()},
            }
        if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
            # Storage state / browser export
            flat: dict[str, str] = {}
            for c in raw["cookies"]:
                if isinstance(c, dict) and "name" in c and "value" in c:
                    flat[str(c["name"])] = str(c["value"])
            return {"main": flat, "live": {}}
        if isinstance(raw, dict):
            # Flat dict: treat all top-level keys as main-site cookies
            flat = {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}
            return {"main": flat, "live": {}}
        return {"main": {}, "live": {}}

    # ── CookieJar Protocol ──────────────────────────────────

    def is_logged_in(self) -> bool:
        """True iff main-site has did AND (passToken OR webday7_st)."""
        self._refresh()
        main = self._data.get("main", {})
        has_did = bool(main.get("did"))
        has_session = bool(
            main.get("passToken") or main.get("kuaishou.server.webday7_st")
        )
        return has_did and has_session

    def as_dict(self, site: str | None = None) -> dict[str, str]:
        """Return a flat ``{name: value}`` dict for the requested site.

        ``site=None`` defaults to ``"main"``. Unknown site names return
        an empty dict.
        """
        self._refresh()
        key = site or DEFAULT_SITE
        return dict(self._data.get(key, {}))

    def as_string(self, site: str | None = None) -> str:
        """Render the requested site's cookies as a ``"k=v; k=v"`` string."""
        return "; ".join(f"{k}={v}" for k, v in self.as_dict(site).items() if v)

    # ── Multi-site accessors ────────────────────────────────

    @property
    def data(self) -> dict[str, dict[str, str]]:
        """Live view of the dual-site cookie dict (read on demand)."""
        self._refresh()
        return {site: dict(self._data.get(site, {})) for site in SITES}

    def get_did(self) -> str:
        """Convenience: return main-site did or empty string."""
        return self.as_dict("main").get("did", "")

    # ── Write path ──────────────────────────────────────────

    def replace_all(self, data: dict[str, dict[str, str]]) -> None:
        """Replace the in-memory cookie state for both sites.

        ``data`` must be ``{"main": {...}, "live": {...}}`` shape — any
        keys outside ``main``/``live`` are silently dropped. Call
        ``save()`` afterwards to persist.
        """
        new_data: dict[str, dict[str, str]] = {"main": {}, "live": {}}
        for site in SITES:
            site_dict = data.get(site, {}) if isinstance(data, dict) else {}
            if isinstance(site_dict, dict):
                new_data[site] = {str(k): str(v) for k, v in site_dict.items()}
        self._data = new_data
        self._loaded = True
        # Defer mtime stamp until save() actually persists.

    def update_site(self, site: str, cookies: dict[str, str]) -> None:
        """Merge ``cookies`` into the given site's dict (overwriting names)."""
        if site not in SITES:
            raise ValueError(f"unknown site: {site!r} (expected one of {SITES})")
        self._refresh()
        bucket = self._data.setdefault(site, {})
        for k, v in (cookies or {}).items():
            bucket[str(k)] = str(v)
        self._data[site] = bucket

    def update_token(self, site: str, name: str, value: str) -> None:
        """Update a single cookie value in the given site."""
        if site not in SITES:
            raise ValueError(f"unknown site: {site!r} (expected one of {SITES})")
        self._refresh()
        bucket = self._data.setdefault(site, {})
        bucket[str(name)] = str(value)
        self._data[site] = bucket

    # ── Origin metadata path（R7 P5）────────────────────────────────
    #
    # 设计契约：
    #   * 读取（has_origin_headers / get_origin_headers）惰性 _refresh，
    #     调用方安全；
    #   * 写入（update_origin_headers）只动内存，必须显式 save() 持久化。
    #     写入时校验 source 必须是已知枚举，杜绝拼错字符串污染 metadata；
    #   * replace_all / update_site / update_token 不动 metadata —— 业务
    #     刷 cookie 不应该意外清空 BBA 抓到的身份头。

    def has_origin_headers(self) -> bool:
        """True iff metadata has at least the UA + sec-ch-ua-platform pair.

        这两个是身份层的最小完备子集——其他字段（mobile / language /
        sec-ch-ua brand list）即使缺失也能由调用方补默认值，但缺 UA 或
        platform 就没意义了。
        """
        self._refresh()
        return bool(
            self._metadata.get("origin_user_agent")
            and self._metadata.get("origin_sec_ch_ua_platform")
        )

    def get_origin_headers(self) -> dict[str, str]:
        """Return the captured/synthesized identity headers.

        返回 {} 表示尚未抓到（调用方应 raise）。注意返回的是副本，
        外部修改不影响 jar 内部状态。
        """
        self._refresh()
        out: dict[str, str] = {}
        for field in ORIGIN_FIELDS:
            v = self._metadata.get(field)
            if isinstance(v, str) and v:
                # 持久化字段名 origin_user_agent → wire 字段名 user-agent 的映射
                wire_key = field.replace("origin_", "").replace("_", "-")
                out[wire_key] = v
        return out

    def get_origin_source(self) -> str:
        """Return ``"captured"`` / ``"synthesized"`` / ``""`` (未设置)."""
        self._refresh()
        v = self._metadata.get("origin_source")
        return v if isinstance(v, str) else ""

    # ── Profile dir binding（R7 P6）────────────────────────────────
    #
    # 设计契约：
    #   * 只动 metadata.profile_dir，不影响 cookie 数据 / origin headers；
    #   * 调用方传相对 data_root 的 POSIX 路径，jar 不做合法性校验
    #     （持久化层不应该知道 data_root 在哪）；
    #   * set_profile_dir 只动内存，必须显式 save() 持久化。

    def get_profile_dir(self) -> str:
        """Return the bound user_data_dir (relative path) or empty string."""
        self._refresh()
        v = self._metadata.get(PROFILE_DIR_FIELD)
        return v if isinstance(v, str) else ""

    def set_profile_dir(self, rel_path: str) -> None:
        """Bind this cookie to a specific user_data_dir.

        Args:
            rel_path: 相对 data_root 的 POSIX 路径字符串，例如
                ``"browser_profiles/kuaishou/_new_20260603_191143"``。
                传空字符串视作显式解绑（fallback 到 cookie_id 算法）。
        """
        self._refresh()
        new_meta: dict[str, str | float] = dict(self._metadata)
        if rel_path:
            new_meta[PROFILE_DIR_FIELD] = str(rel_path)
        else:
            new_meta.pop(PROFILE_DIR_FIELD, None)
        self._metadata = new_meta

    def update_origin_headers(
        self,
        headers: dict[str, str],
        *,
        source: str,
    ) -> None:
        """Replace the metadata block in memory（must call save() to persist）.

        Args:
            headers: wire-format dict（小写 key）, e.g.::

                {
                    "user-agent":          "Mozilla/5.0 ...",
                    "sec-ch-ua":           "\"Chromium\";v=\"148\", ...",
                    "sec-ch-ua-mobile":    "?0",
                    "sec-ch-ua-platform":  "\"Windows\"",
                    "accept-language":     "zh-CN,zh;q=0.9,...",
                }

                未传的字段保留旧值；传空字符串视作显式清除。

            source: 必须是 ``"captured"``（E1 实抓）或 ``"synthesized"``
                （E2 兜底），其他值 raise ValueError。
        """
        if source not in (ORIGIN_SOURCE_CAPTURED, ORIGIN_SOURCE_SYNTHESIZED):
            raise ValueError(
                f"unknown origin source: {source!r} "
                f"(expected {ORIGIN_SOURCE_CAPTURED!r} or "
                f"{ORIGIN_SOURCE_SYNTHESIZED!r})"
            )
        self._refresh()
        new_meta: dict[str, str | float] = dict(self._metadata)
        for wire_key, value in (headers or {}).items():
            field = "origin_" + wire_key.lower().replace("-", "_")
            if field not in ORIGIN_FIELDS:
                # 防御：未知字段静默忽略，不污染持久化结构
                continue
            if value:
                new_meta[field] = str(value)
            else:
                new_meta.pop(field, None)
        new_meta["origin_source"] = source
        new_meta["origin_captured_at"] = time.time()
        self._metadata = new_meta

    def save(self) -> None:
        """Persist current in-memory state back to disk (native shape)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # native shape + optional metadata block（旧 jar 无 metadata 时不写）
        payload: dict[str, object] = dict(self._data)
        if self._metadata:
            payload["metadata"] = dict(self._metadata)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=4)
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            pass

    def __repr__(self) -> str:  # pragma: no cover
        state = "loaded" if self.is_logged_in() else "empty"
        return f"KuaishouCookieJar(path={self._path!r}, state={state})"
