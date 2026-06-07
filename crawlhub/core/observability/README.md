# R7 Observability — Daemon-Level Transport Recorder

> 业务零感知地观测 daemon 内所有出向请求/响应/WSS 帧。  
> Spec ref: R7 (`features/r7-observability/spec.md`)。

## 一、它解决什么问题

爬虫平台内有四条独立网络出口：

| 路径 | 调用栈 | 谁产生 |
|------|--------|--------|
| `urllib3` 同步 HTTP | `requests` / urllib3 直连 | bilibili / qimai / weibo / steam |
| `httpx` 同/异步 HTTP | `httpx.Client` / `httpx.AsyncClient` | 内部 SDK / 部分平台 |
| `websockets` 异步 WSS | `websockets.asyncio` connect / send / recv | 抖音/快手 直播 IM 协议解析 |
| 浏览器内 fetch + WSS | Playwright + CDP | BBA 反爬流程的 GET/POST + 直播间 WSS push |

R7 在 daemon 启动第一行（早于任何业务模块 import）装好 monkey-patch + CDP recorder，把这四条路统一收拢到 `requests.jsonl`。**业务代码零感知，开关 1 个 ENV 关掉**。

## 二、数据存放位置

每个任务一个目录，路径与 `data.jsonl` 共址：

```
<output_dir>/<YYYY-MM-DD>/<task_id>_<platform>_<action>/
├── data.jsonl              # 业务记录（不变）
├── requests.jsonl          # ← R7 新增：daemon 全链路网络观测
├── log.txt
├── summary.json
└── assets/
```

Windows 默认：`C:\Users\<user>\.crawlhub\output\...`  
Linux/macOS 默认：`~/.crawlhub/output/...`

> Lazy 生成：只有任务首次产生 transport 事件时才创建 `requests.jsonl`。  
> 全程没访问任何 URL 的任务（如纯结构化解析）不会产生该文件。

## 三、jsonl Schema

每行一个 JSON record。**嵌套结构**，不是扁平 key。

```json
{
  "ts_ms": 1780299289448,
  "task_id": "f40e1b1f2d29",
  "platform": "douyin",
  "action": "collect_live_events",
  "source": "browser_ws",
  "phase": "ws_recv",

  "method": "GET",
  "url": "wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/?...",
  "url_path": "/webcast/im/push/v2/",
  "url_query_keys": ["app_name", "version_code", ...],

  "request":  { "headers": {...}, "body_preview": null, "body_size": 0 },
  "response": { "status": null, "headers": {}, "body_preview": "<binary>", "body_size": 863,
                "rt_total_ms": null, "rt_first_byte_ms": null },

  "transport": { "library": "cdp", "version": "...", "is_async": false,
                 "http_version": null, "tls": {"version": null, "cipher": null} },

  "correlation": { "ref_id": "rq_a1b2c3d4", "in_flight_count": 0 },

  "extra": { "request_id": "52620.947", "opcode": 2, "frame_size": 617, "mask": false }
}
```

### Source 取值

| `source` | 含义 | 触发场景 |
|----------|------|---------|
| `py_http` | Python 进程内 HTTP（urllib3 / httpx） | 普通 API 直连 |
| `py_ws` | Python 进程内 WSS（`websockets` 库） | 抖音/快手 IM 协议直接连 |
| `browser_network` | 浏览器内 fetch/XHR | BBA 反爬流程内的 HTTP |
| `browser_ws` | 浏览器内 WSS（CDP `Network.webSocket*`） | 直播页同源 WSS push |

### Phase 取值

`request` / `response` / `response_body` / `request_extra` / `response_extra` / `ws_open` / `ws_send` / `ws_recv` / `ws_close`

> ⚠️ **`request` vs `request_extra` 的关键区别**
>
> CDP 协议把一个 HTTP 请求的生命周期切成两条事件流：
>
> | phase | 来源事件 | 包含的 headers | 不包含 |
> |-------|---------|---------------|--------|
> | `request` | `Network.requestWillBeSent` | JS 显式设置的（XHR/fetch caller 传入） | cookie、sec-fetch-*、accept-encoding、UA-CH 派生头 |
> | `request_extra` | `Network.requestWillBeSentExtraInfo` | **浏览器最终发出去的全部 headers** | — |
>
> 同样，`response` 来自 `responseReceived`，`response_extra` 来自 `responseReceivedExtraInfo`（含完整 set-cookie）。
>
> **诊断要点**：要看 cookie / sec-fetch-* / accept-encoding 是否真的发了，**必须看 `phase=request_extra`**，看 `phase=request` 永远缺这些。两条事件靠 `extra.request_id` 关联（消费侧 join），顺序不保证。
>
> **可能缺失**：缓存命中、被取消的请求、某些 Worker 内请求可能不发 ExtraInfo，主流业务请求 99% 都会发。

### 关键路径 cheatsheet

| 想要的字段 | 路径 |
|-----------|------|
| 调用方法 | `method` |
| 完整 URL | `url`（原文，含 msToken / a_bogus 等） |
| HTTP 状态码 | `response.status` |
| 用了哪个库 | `transport.library`（urllib3 / httpx / websockets.asyncio / cdp） |
| **cookie**（实际发出的） | `phase=request_extra` 的 `request.headers.cookie`（原文，可 kv 级 split 对比） |
| **sec-fetch-* / accept-encoding** | `phase=request_extra` 的 `request.headers.*` |
| **set-cookie**（服务器返回的） | `phase=response_extra` 的 `response.headers.set-cookie` |
| Cookie 关联数 | `phase=request_extra` 的 `extra.associated_cookies_count`（CDP 上报关联到此请求的 cookie 总数） |
| Cookie warning（仍发送） | `phase=request_extra` 的 `extra.cookies_with_warning_count`（SameSite 等过渡期警告，cookie 实际仍随请求发送） |
| Cookie 真阻断（没发送） | `phase=request_extra` 的 `extra.cookies_truly_blocked_count`（NotOnPath / DomainMismatch 等硬阻断，扣除 exemption 豁免） |
| Set-Cookie 拒收 | `phase=response_extra` 的 `extra.set_cookies_blocked_count`（服务器返回的 set-cookie 没被写入 jar） |
| 关联 request 与 request_extra | `extra.request_id` 相同 |
| WSS 帧 opcode | `extra.opcode`（1=text, 2=binary） |
| WSS 帧大小 | `extra.frame_size` |
| WSS payload 预览（4KB 截断） | `response.body_preview` |
| 关联 ID | `correlation.ref_id`（同一请求 request/response 对齐） |

## 四、原文落库（无脱敏）

R7 监控的存在意义就是 **kv 级细粒度可观测性** —— 让消费者（viewer / 诊断脚本）能拿
请求 vs cra trace、浏览器实发 vs Python 实发 做字段级对照。脱敏会把 cookie 整串 hash
成 `[redacted len=7053 head=hevc_sup hash=153bfd0b]`，监控直接失去存在意义。

所以：

- URL 完整原文，含 `msToken` / `a_bogus` / `_signature` / `X-Bogus` 等签名 query 全部 value
- `request.headers` / `response.headers` 完整原文，含 `Cookie` / `Set-Cookie` / `Authorization` 全部 value
- 仅 `body_preview` 做 4KB 截断（非脱敏，纯避免吞内存）

**前提假设**：`requests.jsonl` 永远只在本地磁盘流转，不进 git、不分享 zip、不上传日志服务。
任务输出目录在 `~/.crawlhub/output/` 下，由用户自己掌控。如果未来出现需要外发 jsonl
的场景，再单独写一个 `tools/observability/anonymize.py` 后处理脚本，**不要回到默认脱敏**。

> 历史教训（2026-06-02）：原 spec §7.5 定义了一套基于黑名单的默认脱敏，
> 把 cookie 整串 hash、把 msToken value 替换成长度+前缀+SHA1。
> 第一次用 R7 vs cra trace 做对比就发现 cookie kv 数从 67 缩成 1，
> 监控完全用不了 —— 已整体拆除（commit 见本节修改时间点）。

## 五、配置开关

R7 在 daemon 进程里**永远装 patch**（无 kill switch），是否把事件落到 `requests.jsonl`
由全局配置控制：

```yaml
# ~/.crawlhub/config.yaml
observability:
  record_requests: false   # 默认 false：patch 装好但不写文件（零开销监控盲区，节省磁盘）
                           # 调试时改 true，重启 daemon 生效
```

**单一信源**：不再读 ENV 覆盖（历史 ENV `CRAWLHUB_REQUEST_LOG` / `CRAWLHUB_R7_DISABLE` 已废弃）。
要临时开关，直接改 yaml 然后 `crawlhub restart`。

## 六、查看数据

`requests.jsonl` 单文件可达数 MB，直接编辑器打开会卡。配套 CLI 工具：

```bash
# 摘要（总数 / source / phase / transport.library / platform 分布 + ref_id 注入率）
python tools/observability/inspect_requests.py <task_dir>

# 列前 50 条简要（一行一条）
python tools/observability/inspect_requests.py <task_dir> --head 50

# 多重过滤可叠加，规则：key=val 精确 / key~val 子串 / key^val 前缀
python tools/observability/inspect_requests.py <task_dir> \
    --filter source=cdp \
    --filter url~douyin \
    --filter response.status=200 \
    --head 20

# 仅 WSS 帧（含 opcode / size / payload preview）
python tools/observability/inspect_requests.py <task_dir> --ws

# 单条完整 record（按 head 列出的 idx）
python tools/observability/inspect_requests.py <task_dir> --show 42

# 数据健康度（损坏行计数 + 关键字段缺失统计）
python tools/observability/inspect_requests.py <task_dir> --health
```

`<task_dir>` 可以是任务目录，也可以直接传 `requests.jsonl` 本身。

## 七、内部模块图

```
crawlhub/core/observability/
├── __init__.py          # 对外接口：install_all / is_installed
├── install.py           # 装载中枢：无 kill switch（一律装 patch）+ _wrap_method 通用 wrapper
├── http_patches.py      # urllib3 / httpx sync+async / websockets 4 路 patch
├── cdp_recorder.py      # Playwright Page → CDP Network.* + Network.webSocket* 收集
├── records.py           # make_record(): 原文 schema 构造（无脱敏）
└── writer.py            # _RequestsWriter：后台线程 batch flush，队列满 drop 不阻塞业务
```

挂载点：

| 入口 | 调用 |
|------|------|
| `crawlhub/__main__.py` | `install_all()`（早于业务 import） |
| `crawlhub/cli/__init__.py` | 同上 |
| `crawlhub/cli/mcp_server.py` | 同上 |
| `crawlhub/core/browser/provider.py::BrowserSessionProvider.hold()` | `cdp_recorder.attach(raw_page, ctx)` |

## 八、双腿覆盖（CDP + Python WSS）

抖音直播间 WSS push（`wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/`）会 **同时** 被两个 recorder 抓到：

- **CDP 腿**（`source=browser_ws`，`transport.library=cdp`）—— Playwright/Chromium 浏览器层
- **Python 腿**（`source=py_ws`，`transport.library=websockets.asyncio`）—— 业务代码 `await ws.recv()`

设计有意保留两腿。一腿坏了另一腿仍能采，对比双腿可发现：协议解析问题（解码差异）、丢帧（CDP 异步上报丢失）、时序问题（业务消费滞后）。**不要把任意一腿当冗余删掉**。

## 九、性能

- 队列 `maxsize=4096`，满直接 drop（`writer.py::QUEUE_MAX`），不阻塞业务
- 每 256 条或 1s `flush()`，daemon 线程
- 每条 record 经一次 `urlsplit` + `parse_qsl` 提取 query keys（~3μs），可忽略
- WSS 高频帧（直播间 ~50 fps）实测 `_RequestsWriter` 落盘吞吐 >5k records/s

## 十、Rollback

任何一处怀疑 R7 影响业务（性能 / 兼容性 / 副作用）：

```yaml
# ~/.crawlhub/config.yaml
observability:
  record_requests: false   # 关掉 jsonl 写入即可（默认就是 false）
```

然后 `crawlhub restart`。

效果：

- patch 仍然装（无 kill switch；监控盲区为零），但 `record_request()` 第一行就 return
- `attach()` 仍调用，CDP listener 仍挂，只是事件不写文件
- `requests.jsonl` 不会创建，业务 `data.jsonl` 不受影响

> 历史 ENV `CRAWLHUB_R7_DISABLE` / `CRAWLHUB_REQUEST_LOG` 已废弃。如果你执意要彻底跳过
> patch 安装，需要 git revert observability 这条线（spec §11 Plan B-2 流程）。

## 十一、不变量（如果你要修）

1. **`install_all()` 不能在 patch 失败时抛**——观测层崩了不能拖垮业务
2. **`record_request()` 内不能抛**——TaskContext 里失败一次就 disable 整个 task 的写入
3. **CDP `attach()` 必须 idempotent**——同一 page 多次 hold() 不能挂多个 listener
4. **不能基于库版本号 if/else 切片**——用 `try/import` 决定能装就装，不能装就 skip
5. **`PlaywrightPageWrapper` 解包**——CDP attach 入口一定要 `raw = getattr(page, "_page", None) or page` 拿原生 Page
6. **`request` 与 `request_extra` 永远是两条独立 record**——CDP 不保证两个事件的到达顺序，也不保证 ExtraInfo 一定来。**不要尝试 merge**：维护一个 in-flight 缓存等"两条都到再 emit"会引入丢数据的风险（缓存命中走捷径 / page 关闭时未 flush 的中间态）。消费侧靠 `extra.request_id` 自行 join，简单可靠。
7. **`blockedReasons` 非空 ≠ cookie 没发出去**——CDP `BlockedCookieWithReason` 把 SameSite 过渡期 warning（`SameSiteUnspecifiedTreatedAsLax` / `WarnSameSiteNoneInsecure` 等）和真阻断（`NotOnPath` / `DomainMismatch` / `SecureOnly` 等）混在同一个字段。还有 `exemptionReason` 可豁免。绝不要 `if c.get("blockedReasons")` 一刀切判定。分类参考 `cdp_recorder._WARNING_ONLY_BLOCK_REASONS` 和 `_classify_associated_cookies()`。这条铁律是 2026-06-02 一次误报 49% 阻断率的血泪。

第 5 条是 R7 phase 2 的血泪教训：CrawlHub 的 PageHandle 把原生 Playwright Page 包了一层，CDP 拿不到 `.context` / `.once`。这个解包动作在 `cdp_recorder.attach()` 已经做了，**不要在重构时移走**。
