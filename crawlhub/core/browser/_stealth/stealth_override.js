/* ============================================================================
 * crawlhub Stealth Override v2 — 自适应版本
 * ----------------------------------------------------------------------------
 * 设计原则：
 *   1. 不主动添加：真 Chrome 没有的，也别造（chrome.runtime 反例）
 *   2. 只对齐差异：patchright 跟真 Chrome 哪里有差，单独 patch 哪里
 *   3. 跨平台自适应：从 window.__CRAWLHUB_STEALTH_CONFIG__ 读宿主信息，
 *      根据宿主真实 OS/版本决定 patch 什么。这份配置由 Python 端在
 *      add_init_script 之前注入。
 *   4. 用 probe.js diff 驱动：leak 出现 → 加 patch → leak 消失 → commit
 *
 * 配置契约（由 host_environment.py 生成）：
 *   window.__CRAWLHUB_STEALTH_CONFIG__ = {
 *     os: "Windows" | "macOS" | "Linux" | "Unknown",
 *     os_version: "Windows 11 (build 26100)",
 *     platform_version_hint: "19.0.0",
 *     should_patch_platform_version: true,
 *     chrome_major: "148",
 *     ua: "Mozilla/5.0 ...",
 *     screen_width: 2560,    // 宿主逻辑分辨率（Chrome 看到的值）
 *     screen_height: 1440,
 *   }
 *
 * 当前修复列表：
 *   [P0-1] navigator.userAgentData.platformVersion 自适应
 *          - Win11: "10.0" → "19.0.0"（或 should_patch=true 时跟着 config 走）
 *          - Win10: 不改（Chrome 默认就是 "10.0"）
 *          - Mac/Linux: 不改（透传真实值）
 *
 *   [P0-2] navigator.languages: 1-2 项 → 4 项 ["zh-CN","zh","en-US","en"]
 *          launch_kwargs.locale="zh-CN" 副作用：把 languages 砍到 1 项。
 *          所有 OS 都需要这个 patch（locale 是 launch 参数注入的，跟 OS 无关）。
 *
 *   [P0-3] screen.width / screen.height = 宿主真实逻辑分辨率（2026-06-01 新增）
 *          老版本 viewport 写死 1920x1080；2K/4K 宿主上 navigator.screen
 *          会被抖音 acrawler.js 读出来塞 search/single 等接口的 query →
 *          系统性 leak。修复：viewport 跟 stealth.screen.width/height 同源，
 *          都从 host_environment._detect_screen_size() 来。
 *
 *   [P1-1] Worker 上下文同步 patch
 *          Worker 内 navigator.languages 跟 main world 一致。
 *
 *   [P1-2] screen.availHeight 留任务栏 40px（仅 Windows）
 *          Mac 顶部菜单栏 ~24px，但 screen.availTop 处理不一样，先只处理 Win。
 *
 * 不做的事：
 *   ✗ 不 patch navigator.webdriver（已由 _WEBDRIVER_OVERRIDE_JS 独立处理，
 *     无论 skip_stealth 都注入；不再依赖 --disable-blink-features 命令行 flag）
 *   ✗ 不 patch chrome.runtime（真 Chrome 在普通页面就是 undefined）
 *   ✗ 不 patch chrome.app / csi / loadTimes（用 channel="chrome" 后是真的）
 *   ✗ 不 patch navigator.plugins（固定 polyfill 反成"stealth 库指纹"）
 *   ✗ 不 patch WebGL renderer/vendor（channel="chrome" 下是真显卡）
 *
 * ============================================================================
 * 已知诊断信号（不是 bug，是长期备忘）—— 2026-05-29 抖音直播间 audit 发现
 * ============================================================================
 * 在抖音直播间跑 audit 时，相比"日常 Chrome+真号" baseline，crawlhub 缺以下
 * 平台 SDK 注入物（已加 EXPECTED_DIFF 白名单，不算 leak）：
 *
 *   - navigator.SDKNativeWebApi    抖音 webcastSDK 的 native 桥
 *   - navigator.pemrissions        抖音 acrawler.js 的 honeypot（故意拼错"permissions"）
 *   - navigator.vendorSubs         次要副作用字段
 *
 * 含义：抖音 SDK 在 crawlhub 上**降级到了不同代码路径**。指纹层我们已经全过，
 *       但抖音对 crawlhub 的"信任度"仍然低于真 Chrome+真号。
 *
 * 嫌疑维度（按概率排序，未来如业务不稳定时按此查）：
 *   ⭐⭐⭐  cookie 完整性（__ac_signature / ttwid / msToken 等签名 cookie）
 *           [2026-05-29: 已确认 cookie 一致 → 排除]
 *   ⭐⭐⭐  TLS JA3/JA4 指纹（patchright 的 BoringSSL 跟 Chrome 微差）
 *   ⭐⭐    HTTP/2 SETTINGS frame / Sec-Ch-Ua HTTP 头序
 *   ⭐⭐    行为前置路径（直接 goto vs 从主页点进）/ 首次访问 vs 老 profile
 *   ⭐     WebGL/Canvas 像素级一致性（headless 软件渲染特有）
 *
 * ⚠️ 不要试图 polyfill 上面那 3 个 SDK 注入物：
 *   - SDKNativeWebApi 是抖音真实在调用的桥，伪造一个 SDK 一调用就崩
 *   - pemrissions 是 honeypot，polyfill 进去等于跳进抖音的陷阱
 *   - vendorSubs 不重要，强行造反而异常
 *
 * 排查这层差异的工具：
 *   - SessionRecorder（tools/session_recorder/，开发中）：录两次会话做 diff
 *   - probe_douyin.js / probe_kuaishou.js（待写）：平台特定深度检测
 * ============================================================================
 */

(() => {
  // ⚠️ 不再做防重入：每次 navigation 都让所有 patch 重新跑一遍。
  //   Object.defineProperty 第二次跑会抛 TypeError（property 已存在），
  //   所以下面每个 patch 都用 try{} 包好；命中就更新 getter，没命中静默吞错。
  //   这比"防重入" 更鲁棒：navigation 后 main world 重置，原本的 patch 失效，
  //   旧版的防重入锁会让新 main world 直接跳过，patch 失效（27332fe leak 根因）。
  const STEALTH_VERSION = "2026-06-01.1";  // 改 patch 时 bump，便于 probe 追踪

  // 读 Python 端注入的配置；如果没有就走"中性 fallback"——不做任何 OS 特定 patch
  const cfg = (typeof globalThis.__CRAWLHUB_STEALTH_CONFIG__ === 'object'
                && globalThis.__CRAWLHUB_STEALTH_CONFIG__) || {
    os: "Unknown",
    platform_version_hint: "10.0",
    should_patch_platform_version: false,
  };

  const isMainWorld = typeof window !== 'undefined' && typeof document !== 'undefined';
  const isWindows = cfg.os === "Windows";

  // 自检 marker：probe.js / fingerprint_audit 可以读这个字段判断 stealth 是否生效
  const applied = {
    languages_4: false,
    platform_version_patched: false,
    worker_patched: false,
    screen_avail_patched: false,
    screen_size_patched: false,
    chrome_app_polyfilled: false,
  };

  // ============================================================
  // [P0-1] userAgentData.platformVersion 自适应
  // ============================================================
  // 仅在 should_patch_platform_version=true 时改写（即 Win11 场景）。
  // Mac/Linux/Win10 透传真实值，不动。
  //
  // ⚠️ 2026-05-29 third fix（probe diff 暴露 patch 实际未生效）：
  //   之前用 `navigator.userAgentData.getHighEntropyValues = wrapped`
  //   赋值给实例属性 —— 在 Chrome 内部，navigator.userAgentData 的原型链
  //   是 NavigatorUAData.prototype，方法走的是 prototype 路径。给实例直接
  //   赋值在某些 Chrome 版本（148+）下被引擎绕过 / 或 navigator 在
  //   navigation 后被替换。
  //   修复：用 prototype 路径 Object.defineProperty(NavigatorUAData.prototype, ...)
  //   一刀切到所有实例。同时保留实例路径作为 fallback。
  if (cfg.should_patch_platform_version) {
    try {
      const uad = (typeof navigator !== 'undefined') && navigator.userAgentData;
      if (uad && typeof uad.getHighEntropyValues === 'function') {
        // 拿原型链上真正的 getHighEntropyValues 作为 fallback orig
        const proto = Object.getPrototypeOf(uad);
        const protoOrig = (proto && proto.getHighEntropyValues) || uad.getHighEntropyValues;

        const makeWrapper = function(origFn) {
          // origFn 在调用时绑到具体的 uad 实例（this）
          return async function(hints) {
            const result = await origFn.call(this, hints);
            if (result && typeof result === 'object') {
              if ('platformVersion' in result) {
                const cur = result.platformVersion;
                if (cur === '10.0' || cur === '10.0.0' || cur === '0.0.0' || cur === '') {
                  result.platformVersion = cfg.platform_version_hint;
                }
              }
              if (Array.isArray(result.brands)) {
                result.brands = result.brands.filter(b =>
                  !/HeadlessChrome/i.test(b && b.brand || ''));
              }
              if (Array.isArray(result.fullVersionList)) {
                result.fullVersionList = result.fullVersionList.filter(b =>
                  !/HeadlessChrome/i.test(b && b.brand || ''));
              }
            }
            return result;
          };
        };

        const wrapped = makeWrapper(protoOrig);
        try {
          Object.defineProperty(wrapped, 'toString', {
            value: () => 'function getHighEntropyValues() { [native code] }',
            configurable: true,
          });
        } catch (e) {}

        // 路径 A：改 prototype（一劳永逸，所有 NavigatorUAData 实例都吃 patch）
        let patchedViaProto = false;
        if (proto) {
          try {
            Object.defineProperty(proto, 'getHighEntropyValues', {
              value: wrapped,
              configurable: true,
              writable: true,
            });
            patchedViaProto = true;
            applied.platform_version_patched = true;
          } catch (e) {}
        }

        // 路径 B：兜底，给当前 uad 实例也设一份（万一 prototype 在内部被锁）
        if (!patchedViaProto) {
          try {
            uad.getHighEntropyValues = wrapped;
            applied.platform_version_patched = true;
          } catch (e) {
            try {
              Object.defineProperty(uad, 'getHighEntropyValues', {
                value: wrapped, configurable: true, writable: true,
              });
              applied.platform_version_patched = true;
            } catch (e2) {}
          }
        }
      }
    } catch (e) {}
  }

  // ============================================================
  // [P0-2] navigator.languages 强制 4 项（所有 OS 都需要）
  // ============================================================
  // launch_kwargs.locale="zh-CN" 副作用：navigator.languages 只剩 ["zh-CN"]。
  // 用 configurable:true 让重复 navigation 时能直接覆盖（getter 是新闭包）。
  try {
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: function() { return ['zh-CN', 'zh', 'en-US', 'en']; },
      configurable: true,
    });
    applied.languages_4 = true;
  } catch (e) {}

  // ============================================================
  // [P1-1] Worker 上下文 patch
  // ============================================================
  // 通过劫持 Worker 构造器，把 patch 代码作为前缀拼到 worker code 里。
  //
  // ⚠️ 2026-05-29 third fix（probe diff 暴露 worker 内 languages 仍是 2 项）：
  //   旧版本对 blob: URL 直接 bypass 走 OrigWorker —— 这是大坑：
  //   probe.js 用的就是 `new Worker(URL.createObjectURL(blob))`，
  //   现代 web 框架（vue/react devtool / sentry / 各家 SDK）也都用 blob URL
  //   起 worker。bypass 等于这块完全没 patch。
  //   修复：blob: URL 也拦截 —— 同步 XHR fetch blob 内容（blob: URL 同源
  //   且支持 sync XHR），把 patch 前缀拼好后**重新打包成新 blob URL**。
  if (isMainWorld) {
    try {
      const OrigWorker = window.Worker;
      // PV 配置作为字面量嵌入（worker 里读不到主线程的 __CRAWLHUB_STEALTH_CONFIG__）
      const shouldPatchPV = cfg.should_patch_platform_version ? 'true' : 'false';
      const pvHint = JSON.stringify(cfg.platform_version_hint || '10.0');
      const PATCH_CODE = `
        try {
          // Worker 里没有 Navigator，只有 WorkerNavigator。直接通过实例
          // 拿原型，覆盖所有 OS / Worker 类型（DedicatedWorker / Shared / Service）。
          (function patchLangs(){
            try {
              if (typeof navigator === 'undefined') return;
              var proto = Object.getPrototypeOf(navigator);
              if (!proto) return;
              Object.defineProperty(proto, 'languages', {
                get: function() { return ['zh-CN', 'zh', 'en-US', 'en']; },
                configurable: true,
              });
            } catch(_e) {}
          })();
        } catch(e) {}
        try {
          if (${shouldPatchPV} && typeof navigator !== 'undefined' &&
              navigator.userAgentData &&
              typeof navigator.userAgentData.getHighEntropyValues === 'function') {
            const __proto = Object.getPrototypeOf(navigator.userAgentData);
            const __orig = (__proto && __proto.getHighEntropyValues) ||
                           navigator.userAgentData.getHighEntropyValues;
            const __wrapped = async function(h) {
              const r = await __orig.call(this, h);
              if (r && (r.platformVersion === '10.0' || r.platformVersion === '0.0.0')) {
                r.platformVersion = ${pvHint};
              }
              return r;
            };
            try {
              if (__proto) {
                Object.defineProperty(__proto, 'getHighEntropyValues', {
                  value: __wrapped, configurable: true, writable: true,
                });
              } else {
                navigator.userAgentData.getHighEntropyValues = __wrapped;
              }
            } catch(_e) {
              navigator.userAgentData.getHighEntropyValues = __wrapped;
            }
          }
        } catch(e) {}
      `;

      // 把任意 worker scriptURL 改造成"前缀拼了 PATCH_CODE 的新 blob: URL"
      function buildPatchedBlobUrl(scriptURL) {
        const url = String(scriptURL);
        let body = '';
        if (url.startsWith('blob:')) {
          // blob URL 同源，sync XHR 是允许的（worker 启动本来也卡线程）
          try {
            const xhr = new XMLHttpRequest();
            xhr.open('GET', url, false /* sync */);
            xhr.send();
            body = String(xhr.responseText || '');
          } catch (e) {
            // 拿不到内容就退化为 importScripts —— blob URL 大概率
            // 跨 origin 时 importScripts 会自己抛，只能放弃 patch
            body = `importScripts(${JSON.stringify(url)});`;
          }
        } else {
          // 普通 URL：用 importScripts 远程加载
          body = `importScripts(${JSON.stringify(url)});`;
        }
        const wrappedCode = PATCH_CODE + '\n' + body;
        const blob = new Blob([wrappedCode], { type: 'application/javascript' });
        return URL.createObjectURL(blob);
      }

      function PatchedWorker(scriptURL, options) {
        try {
          // 诊断 marker：每次拦到 Worker 构造，记录一次
          try {
            const m = globalThis.__crawlhub_stealth_marker__;
            if (m) {
              m.worker_intercepts = (m.worker_intercepts || 0) + 1;
              m.last_worker_url_kind = String(scriptURL).startsWith('blob:')
                ? 'blob' : 'url';
            }
          } catch(_e) {}
          const newUrl = buildPatchedBlobUrl(scriptURL);
          return new OrigWorker(newUrl, options);
        } catch (e) {
          // 失败兜底：直接走原版，至少不破坏业务
          try {
            const m = globalThis.__crawlhub_stealth_marker__;
            if (m) {
              m.worker_intercept_errors = (m.worker_intercept_errors || 0) + 1;
              m.last_worker_intercept_error = String(e && e.message || e).slice(0, 200);
            }
          } catch(_e) {}
          return new OrigWorker(scriptURL, options);
        }
      }
      PatchedWorker.prototype = OrigWorker.prototype;
      try {
        Object.defineProperty(PatchedWorker, 'toString', {
          value: () => 'function Worker() { [native code] }',
          configurable: true,
        });
      } catch (e) {}
      window.Worker = PatchedWorker;
      applied.worker_patched = true;
    } catch (e) {}
  }

  // ============================================================
  // [P1-2] screen.availHeight 留任务栏 40px（仅 Windows）
  // ============================================================
  if (isWindows) {
    try {
      if (typeof screen !== 'undefined' && screen.height >= 100) {
        const realHeight = screen.height;
        Object.defineProperty(Screen.prototype, 'availHeight', {
          get: function() { return realHeight - 40; },
          configurable: true,
        });
        Object.defineProperty(Screen.prototype, 'availWidth', {
          get: function() { return screen.width; },
          configurable: true,
        });
        Object.defineProperty(Screen.prototype, 'availLeft', {
          get: function() { return 0; },
          configurable: true,
        });
        Object.defineProperty(Screen.prototype, 'availTop', {
          get: function() { return 0; },
          configurable: true,
        });
        applied.screen_avail_patched = true;
      }
    } catch (e) {}
  }

  // ============================================================
  // [P0-3] screen.width / screen.height = 宿主真实逻辑分辨率
  // ============================================================
  // viewport 已经被 playwright 设置成 host_info.screen_width/height，
  // 大多数情况下 navigator.screen.width/height 会自动跟 viewport 走 —— 但
  // 在 patchright 的某些路径下（特别是 --headless=new + persistent_context），
  // screen.width/height 可能跟 viewport 解耦，回到默认值。
  //
  // 直接强 patch 一次，让 SDK 拿到的永远是宿主真值（与 _wr_request 里
  // 真实浏览器观察值对齐）：
  //   2K 宿主 → 2560x1440
  //   4K 宿主 → 3840x2160（Chrome 在 DPI 150% 下报 2560x1440）
  //
  // 仅在 cfg 里有有效值时 patch，否则透传 patchright 默认（避免无效写入）。
  if (cfg.screen_width && cfg.screen_height &&
      cfg.screen_width >= 800 && cfg.screen_height >= 600) {
    try {
      Object.defineProperty(Screen.prototype, 'width', {
        get: function() { return cfg.screen_width; },
        configurable: true,
      });
      Object.defineProperty(Screen.prototype, 'height', {
        get: function() { return cfg.screen_height; },
        configurable: true,
      });
      // window.innerWidth/Height 通常等于 viewport，不动；innerWidth 等
      // 由 patchright viewport 设置自动同步。
      applied.screen_size_patched = true;
    } catch (e) {}
  }

  // ============================================================
  // [P1-4] screen.colorDepth / pixelDepth 强制 32（修 headless 模式 leak）
  // ============================================================
  // headless 模式下没有真实显卡 framebuffer，Chrome 退到 24bit 颜色 →
  // 跟 headful / 真用户的 32bit 不一致。反爬库会 cross-check 这两项跟
  // WebGL/Canvas 输出一致性。强制改 32 让它跟主流真用户对齐。
  // headful 模式下宿主本来就是 32，这个 patch 在 headful 是 no-op。
  try {
    if (typeof screen !== 'undefined') {
      Object.defineProperty(Screen.prototype, 'colorDepth', {
        get: function() { return 32; },
        configurable: true,
      });
      Object.defineProperty(Screen.prototype, 'pixelDepth', {
        get: function() { return 32; },
        configurable: true,
      });
    }
  } catch (e) {}

  // ============================================================
  // [P1-3] window.chrome.app polyfill
  // ============================================================
  // 真 Chrome 即使在普通页面也会暴露 window.chrome.app 这个对象（虽然 API
  // 早被废弃，但占位对象始终存在）。Patchright 启动的 Chrome 上这个字段
  // 不存在 —— 这是反爬指纹断言点 (typeof window.chrome.app === 'undefined'
  // 反而暴露)。
  // 真 Chrome 里 chrome.app 大致结构：
  //   chrome.app = { isInstalled: false, getDetails: fn, getIsInstalled: fn,
  //                  installState: fn, runningState: fn,
  //                  InstallState: { ... enum }, RunningState: { ... enum } }
  // 我们 polyfill 一份最小可用结构（不抛错、行为接近真值）。
  if (isMainWorld) {
    try {
      if (typeof window.chrome === 'object' && window.chrome &&
          typeof window.chrome.app === 'undefined') {
        const InstallState = Object.freeze({
          DISABLED: 'disabled',
          INSTALLED: 'installed',
          NOT_INSTALLED: 'not_installed',
        });
        const RunningState = Object.freeze({
          CANNOT_RUN: 'cannot_run',
          READY_TO_RUN: 'ready_to_run',
          RUNNING: 'running',
        });
        const _appShim = {
          isInstalled: false,
          InstallState: InstallState,
          RunningState: RunningState,
          // 这些函数真 Chrome 会抛"chrome.app.X is deprecated"，但 typeof
          // 是 'function'。指纹检测一般只看 typeof，不会真调用。
          getDetails: function() {
            throw new Error("Cannot read properties of undefined (reading 'getDetails')");
          },
          getIsInstalled: function() {
            throw new Error("Cannot read properties of undefined (reading 'getIsInstalled')");
          },
          installState: function() {
            throw new Error("Cannot read properties of undefined (reading 'installState')");
          },
          runningState: function() {
            return 'cannot_run';
          },
        };
        // 让 toString() 看起来像 native
        ['getDetails', 'getIsInstalled', 'installState', 'runningState'].forEach(function(fn){
          try {
            Object.defineProperty(_appShim[fn], 'toString', {
              value: function() { return 'function ' + fn + '() { [native code] }'; },
              configurable: true,
            });
          } catch(_e) {}
        });
        try {
          // 直接赋值；如果 chrome 对象是 frozen 的就走 defineProperty
          window.chrome.app = _appShim;
        } catch (_e) {
          try {
            Object.defineProperty(window.chrome, 'app', {
              value: _appShim, configurable: true, writable: true, enumerable: true,
            });
          } catch(_e2) {}
        }
        applied.chrome_app_polyfilled = (typeof window.chrome.app === 'object');
      } else if (typeof window.chrome === 'object' && window.chrome &&
                 typeof window.chrome.app !== 'undefined') {
        // 已有就标记成功（不覆盖原生）
        applied.chrome_app_polyfilled = true;
      }
    } catch (e) {}
  }

  // ============================================================
  // 自检 marker：让 probe.js / fingerprint_audit 直接读到 stealth 状态
  // ============================================================
  // 这个字段在每次 stealth 重跑时都会被覆盖，applied_at 反映最近一次跑的时刻。
  try {
    globalThis.__crawlhub_stealth_marker__ = {
      version: STEALTH_VERSION,
      applied_at: new Date().toISOString(),
      cfg_os: cfg.os,
      cfg_pv_hint: cfg.platform_version_hint,
      cfg_should_patch_pv: !!cfg.should_patch_platform_version,
      applied: applied,
    };
  } catch (e) {}

})();
