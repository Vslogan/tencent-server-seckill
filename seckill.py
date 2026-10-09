#!/usr/bin/env python3
"""
腾讯云服务器抢购终极方案 — 浏览器内注入JS直接调API
==================================================
在浏览器上下文中用 fetch() 调用 do-goods API，
浏览器自动处理 cookie、CSRF、所有认证。

比纯 HTTP 快（不需要自己处理认证），比 DOM 点击快（不等面板渲染）。

使用:
  python3 seckill.py                  # 抢最近一场
  python3 seckill.py --time 10:00     # 指定时间
  python3 seckill.py --loop           # 持续运行
  python3 seckill.py --test           # 测试(用服务器专区商品)
"""

import asyncio
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError:
    print("pip3 install playwright")
    sys.exit(1)

# ============================================================
# 配置
# ============================================================

ACTIVITY_URL = "https://cloud.tencent.com/act/pro/featured-202607"

# API 地址
DO_GOODS_URL = "https://act-api.cloud.tencent.com/dianshi/do-goods"

# 抢购商品
FLASH_SALE = {
    "act_id": 1897632168296710,
    "activity_id": 164461404341040,
    "type": "lighthouse_v5",
    "name": "轻量4核4G3M (38元/年)",
    "goods_param": {
        "BlueprintId": "LINUX_UNIX",
        "area": 1,
        "ddocUnionConnect": 0,
        "goodsNum": 1,
        "imageId": "lhbp-eqora508",
        "scenario": "0",
        "timeSpanUnit": "12m",
        "zone": "",
        "regionId": 8,  # 地域: 4=上海 8=北京 1=广州 (area=1 内地)
        "type": "bundle_budget_mc_lg4_01",
    },
}

# 测试商品 (服务器专区)
TEST_GOODS = {
    "act_id": 1865957483429697,
    "activity_id": 164461404341040,
    "type": "lighthouse_v5",
    "name": "轻量4核4G3M (服务器专区)",
    "goods_param": {
        "BlueprintId": "LINUX_UNIX",
        "area": 1,
        "ddocUnionConnect": 0,
        "goodsNum": 1,
        "imageId": "lhbp-eqora508",
        "scenario": "0",
        "timeSpanUnit": "12m",
        "zone": "",
        "regionId": 8,  # 地域: 4=上海 8=北京 1=广州 (area=1 内地)
        "type": "bundle_budget_mc_lg4_01",
    },
}

SECKILL_TIMES = ["10:00", "15:00"]

# 并发请求数
CONCURRENCY = 10

# 持续发送时间 (秒)
DURATION_SEC = 10

# 每轮间隔 (毫秒)
ROUND_INTERVAL_MS = 20

# 提前多少毫秒开始发请求 (负数=提前，-3000=提前3秒)
ADVANCE_MS = -3000

# 提前多少秒开始预热连接 (在目标时间前)
WARMUP_SECONDS = 10

STATE_PATH = Path(__file__).parent / ".state.json"
LOG_PATH = Path(__file__).parent / "seckill.log"

# 启动顺序：先用本机的，都没装就回落到 playwright 下载的 chromium
BROWSER_CHANNELS = ["chrome", "msedge", None]

BROWSER_LABEL = {
    "chrome": "本机 Chrome",
    "msedge": "本机 Edge",
    None: "playwright chromium",
}

BROWSER_PATHS = {
    "win": {
        "chrome": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ],
        "msedge": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
        ],
    },
    "darwin": {
        "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "msedge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    },
    "linux": {
        "chrome": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"],
        "msedge": ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"],
    },
}

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]


def detect_browser():
    """看本机装了哪些浏览器，返回第一个找到的 channel。没找到返回 None。"""
    plat = "win" if sys.platform.startswith("win") else (
        "darwin" if sys.platform == "darwin" else "linux")
    paths = BROWSER_PATHS.get(plat, {})

    for channel in BROWSER_CHANNELS:
        if channel is None:
            continue
        for p in paths.get(channel, []):
            if Path(os.path.expandvars(p)).exists():
                return channel

    return None


async def launch_browser(pw, headless=True, extra_args=None):
    """按 BROWSER_CHANNELS 顺序试，谁能起就用谁。"""
    args = LAUNCH_ARGS + list(extra_args or [])
    last = None

    for channel in BROWSER_CHANNELS:
        kwargs = {"headless": headless, "args": args}
        if channel:
            kwargs["channel"] = channel
        try:
            browser = await pw.chromium.launch(**kwargs)
            log(f"使用浏览器: {BROWSER_LABEL[channel]}")
            return browser
        except Exception as e:
            last = e
            if channel is None:
                log(f"启动失败: {e}")
                log("没装本机 Chrome/Edge 的话，执行 python -m playwright install chromium")

    raise RuntimeError(f"没有可用的浏览器: {last}")


# 前端的 x-csrf-token 是动态生成的，得 hook 住 fetch/XHR 才能拿到
CSRF_HOOK_JS = """
window.__latestCsrf = null;
const _fetch = window.fetch;
window.fetch = function(...args) {
    try {
        const h = (args[1] && args[1].headers) || {};
        if (h['x-csrf-token']) window.__latestCsrf = h['x-csrf-token'];
    } catch(e) {}
    return _fetch.apply(this, args);
};
const _open = XMLHttpRequest.prototype.open;
const _set = XMLHttpRequest.prototype.setRequestHeader;
XMLHttpRequest.prototype.open = function(m, u) {
    return _open.apply(this, arguments);
};
XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
    if (String(k).toLowerCase() === 'x-csrf-token') window.__latestCsrf = v;
    return _set.apply(this, arguments);
};
"""


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}")
    try:
        # 中文 Windows 默认 cp936，编不了 emoji，不指定 encoding 会 UnicodeEncodeError
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except UnicodeEncodeError:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")


async def sync_time(page):
    """对时：7次采样 act-api 响应头 Date 取中位数（秒精度噪声±500ms → 收敛到±0.2s），失败回退页面 nowTime"""
    import email.utils
    SAMPLES = 7
    offsets = []

    async def one_sample():
        fut = asyncio.get_event_loop().create_future()

        def on_resp(resp):
            if fut.done():
                return
            if "act-api" in resp.url:
                d = resp.headers.get("date") or resp.headers.get("Date")
                if d:
                    try:
                        dt = email.utils.parsedate_to_datetime(d)
                        fut.set_result(int(dt.timestamp() * 1000) - int(time.time() * 1000))
                    except Exception:
                        pass

        page.on("response", on_resp)
        try:
            # 主动触发一次 act-api 查询请求（get-vip-info，无害查询接口）
            await page.evaluate("""
                (url) => fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'x-csrf-token': window.__latestCsrf || ''},
                    credentials: 'include', body: '{}'
                }).then(r => r.json()).catch(e => ({}))
            """, "https://act-api.cloud.tencent.com/user/get-vip-info")
            off = await asyncio.wait_for(fut, timeout=3)
            offsets.append(off)
        except Exception:
            pass
        finally:
            page.remove_listener("response", on_resp)

    for _ in range(SAMPLES):
        await one_sample()
        await asyncio.sleep(0.15)
    if len(offsets) < SAMPLES:
        log(f"对时采样成功 {len(offsets)}/{SAMPLES} 次，失败的采样未计入")

    if offsets:
        offsets.sort()
        offset = int(offsets[len(offsets) // 2])
        log(f"服务器时间偏移: {offset:+d}ms (Date头对时 ×{len(offsets)}中位数 {offsets})")
        return offset

    # 回退：页面 nowTime（静态值，含页面加载延迟，精度较差）
    offset = await page.evaluate("""
        () => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const m = (s.textContent||'').match(/"nowTime"\\s*:\\s*(\\d{13})/);
                if (m) return parseInt(m[1]) - Date.now();
            }
            return 0;
        }
    """)
    log(f"服务器时间偏移: {offset:+d}ms (nowTime回退)")
    return offset


# ============================================================
# do-goods 请求与结果记录（调试日志）
# ============================================================

def dlog(msg):
    """只写入日志文件、不输出到控制台：逐次请求的详细记录，避免刷屏"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] [DEBUG] {msg}\n")
    except UnicodeEncodeError:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{datetime.now().isoformat()}] [DEBUG] {msg}\n")


# 单次 do-goods 请求，在页面上下文执行（Cookie / CSRF 由浏览器带上）。
# 返回对象 = 服务端 JSON + 调试字段：
#   _http     HTTP 状态码（网络异常为 0）
#   _ms       请求耗时（毫秒）
#   _csrf_len 发送时 x-csrf-token 的长度（为 0 说明 hook 还没捕获到 token）
# 非 JSON 响应（登录页 / 风控页等）和网络异常也包装成带 code 的对象，统一记录原因。
DO_ONE_JS = """
async (params) => {
    const t0 = performance.now();
    const csrf = window.__latestCsrf || '';
    const dbg = {_csrf_len: csrf.length};
    try {
        const resp = await fetch(params.url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'x-csrf-token': csrf},
            credentials: 'include',
            body: JSON.stringify({
                activity_id: params.activity_id,
                goods: [{act_id: params.act_id, type: params.type, goods_param: params.goods_param}],
                agent_channel: {fromChannel: '', fromSales: '', fromUrl: '', isAgentClient: false},
                preview: 0,
            }),
        });
        const text = await resp.text();
        const ms = Math.round(performance.now() - t0);
        let j = null;
        try { j = JSON.parse(text); } catch (e) { j = null; }
        if (j === null || typeof j !== 'object' || Array.isArray(j)) {
            return {...dbg, code: 'HTTP' + resp.status + '-非JSON', msg: '响应不是 JSON',
                    _http: resp.status, _ms: ms, _raw: text.slice(0, 300)};
        }
        return {...j, ...dbg, _http: resp.status, _ms: ms};
    } catch (e) {
        return {...dbg, code: -1, msg: 'fetch异常: ' + e.message,
                _http: 0, _ms: Math.round(performance.now() - t0)};
    }
}
"""

# 一批 CONCURRENCY 个并发请求
BATCH_JS = ("(params) => Promise.all(Array.from({length: params.concurrency}, () => ("
            + DO_ONE_JS.strip() + ")(params)))")


def _do_goods_params(goods):
    return {
        "url": DO_GOODS_URL,
        "activity_id": goods["activity_id"],
        "act_id": goods["act_id"],
        "type": goods["type"],
        "goods_param": goods.get("goods_param", {}),
        "concurrency": CONCURRENCY,
    }


def _reason_key(r):
    """把一次返回归类成一个原因键，用于统计分布"""
    return f"code={r.get('code')} msg={str(r.get('msg') or '')[:60]}"


def _brief(r):
    return f"{_reason_key(r)} http={r.get('_http')} {r.get('_ms')}ms"


def _snippet(r, limit=400):
    """完整原文（截断），用于判断具体原因"""
    try:
        s = json.dumps(r, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(r)
    return s if len(s) <= limit else s[:limit] + "...(截断)"


class PurchaseStats:
    """统计每次抢购请求的返回，抢购结束时输出原因分布"""

    def __init__(self):
        self.total = 0
        self.counts = {}      # 原因键 -> 次数
        self.samples = {}     # 原因键 -> 首次出现的原文
        self.http = {}        # HTTP 状态 -> 次数
        self.latency = []     # 每次请求耗时(ms)
        self.csrf_empty = 0   # 发送时 CSRF token 为空的次数

    def add(self, r):
        key = _reason_key(r)
        self.total += 1
        self.counts[key] = self.counts.get(key, 0) + 1
        self.samples.setdefault(key, _snippet(r))
        h = str(r.get("_http"))
        self.http[h] = self.http.get(h, 0) + 1
        if isinstance(r.get("_ms"), (int, float)):
            self.latency.append(r["_ms"])
        if r.get("_csrf_len") == 0:
            self.csrf_empty += 1
        return key


def _diagnose(stats):
    """根据返回分布给出未抢到的可能原因（只做提示，结论以原文为准）"""
    if stats.total == 0:
        return "一次请求都没有发出"
    keys = list(stats.counts)
    hints = []
    if len(keys) == 1:
        hints.append(f"全部 {stats.total} 次请求返回相同结果")
    if stats.csrf_empty:
        hints.append(f"{stats.csrf_empty}/{stats.total} 次请求发送时 CSRF token 为空，"
                     "请检查活动页是否加载完成、登录态是否有效")
    if any(k.startswith("code=-1") for k in keys):
        hints.append("存在 fetch 网络异常（连接中断、页面跳转等）")
    if any("非JSON" in k for k in keys):
        hints.append("服务端返回了非 JSON 响应（可能是登录过期、风控或网关拦截）")
    non200 = sum(n for h, n in stats.http.items() if h not in ("200", "0"))
    if non200:
        hints.append(f"{non200} 次 HTTP 状态非 200，见上方 HTTP 分布")
    if any("登录" in k for k in keys):
        hints.append("返回信息提示需要登录，登录态可能已失效")
    if not hints:
        hints.append("请结合上方各类返回的 msg 原文判断")
    return "；".join(hints)


def _log_purchase_summary(stats, success):
    if stats.total == 0:
        log("抢购统计: 没有任何请求返回")
        return
    log(f"抢购统计: 共 {stats.total} 次请求，返回分布:")
    for key, n in sorted(stats.counts.items(), key=lambda kv: -kv[1]):
        log(f"  {n:>6} 次  {key}")
        log(f"           原文示例: {stats.samples[key]}")
    log(f"  HTTP 状态分布: {stats.http}")
    if stats.latency:
        lat = sorted(stats.latency)
        log(f"  请求耗时(ms): 最小 {lat[0]} / 中位 {lat[len(lat) // 2]} / 最大 {lat[-1]}")
    if not success:
        log(f"未抢到原因判断: {_diagnose(stats)}")


async def api_do_goods(page, goods, attempt):
    """单次 do-goods 请求（预热用），返回是否下单成功，并记录结果/原因"""
    r = await page.evaluate(DO_ONE_JS, _do_goods_params(goods))
    dlog(f"单次请求 #{attempt} 原文: {_snippet(r, 1000)}")

    if r.get("code") == 0:
        deal = r.get("data") or {}
        log(f"下单成功 #{attempt}")
        log(f"   订单号: {(deal.get('deal_names') or ['?'])[0]}")
        log(f"   大单号: {deal.get('big_deal_no', '?')}")
        return True

    log(f"  请求 #{attempt} 未成功: {_brief(r)}")
    dlog(f"  未成功 msg 全文: {r.get('msg')}")
    return False


async def warmup(page, goods):
    """预热连接：提前发一个请求建立TCP/TLS，到点直接用"""
    log("预热连接...")
    result = await api_do_goods(page, goods, 0)
    log(f"  预热完成 (下单结果: {'成功' if result else '未成功，原因见上'})")


async def rapid_purchase(page, goods, duration=DURATION_SEC):
    """并发调 API，每次 evaluate 发一批请求。逐轮记录结果，结束时输出原因统计"""
    params = _do_goods_params(goods)
    gp = goods.get("goods_param", {})
    log(f"开始抢购：并发={CONCURRENCY}，间隔={ROUND_INTERVAL_MS}ms，持续={duration}s")
    log(f"请求参数: act_id={goods['act_id']} activity_id={goods['activity_id']} "
        f"type={goods['type']} regionId={gp.get('regionId')}")

    stats = PurchaseStats()
    seen = set()
    start = time.monotonic()
    attempt = 0
    rounds = 0
    success = False

    try:
        while time.monotonic() - start < duration:
            rounds += 1
            t0 = time.monotonic()
            batch = await page.evaluate(BATCH_JS, params)
            batch_ms = (time.monotonic() - t0) * 1000
            attempt += len(batch)

            for r in batch:
                key = stats.add(r)
                if key not in seen:
                    seen.add(key)
                    log(f"  [第{rounds}轮] 新的返回类型: {key}")
                    log(f"           原文: {stats.samples[key]}")

            # 每轮的完整记录只写文件
            dlog(f"[第{rounds}轮 批耗时{batch_ms:.0f}ms] " + " | ".join(_brief(r) for r in batch))

            ok = [r for r in batch if r.get("code") == 0]
            if ok:
                deal = ok[0].get("data") or {}
                log(f"下单成功！第{rounds}轮（累计 {attempt} 次请求，本轮成功 {len(ok)}/{len(batch)} 条）")
                log(f"   订单号: {(deal.get('deal_names') or ['?'])[0]}")
                log(f"   大单号: {deal.get('big_deal_no', '?')}")
                dlog(f"   成功原文: {_snippet(ok[0], 1000)}")
                success = True
                break

            if rounds % 20 == 0:
                log(f"  已尝试 {attempt} 次 ({time.monotonic() - start:.1f}s)")

            await asyncio.sleep(ROUND_INTERVAL_MS / 1000)
    finally:
        # 无论正常结束、被中断还是异常，都输出统计，方便定位原因
        elapsed = time.monotonic() - start
        log(f"抢购结束: {attempt} 次请求, {elapsed:.1f}s, {'成功' if success else '未抢到'}")
        _log_purchase_summary(stats, success)

    return success


def find_nearest_time(offset_ms=0):
    """找最近的抢购场次"""
    now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
    for t in SECKILL_TIMES:
        candidate = datetime.strptime(f"{now.date()} {t}", "%Y-%m-%d %H:%M")
        if candidate > now + timedelta(seconds=5):
            return candidate
    tomorrow = now.date() + timedelta(days=1)
    return datetime.strptime(f"{tomorrow} {SECKILL_TIMES[0]}", "%Y-%m-%d %H:%M")


async def run(target_time=None, test_mode=False, loop_mode=False):
    pw = await async_playwright().start()
    browser = await launch_browser(pw, headless=False)

    if STATE_PATH.exists():
        ctx = await browser.new_context(
            storage_state=str(STATE_PATH),
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
    else:
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )

    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    await ctx.add_init_script(CSRF_HOOK_JS)
    page = await ctx.new_page()
    page.set_default_timeout(30000)

    try:
        # 加载页面
        log("加载活动页...")
        await page.goto(ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 检查登录
        if "login" in page.url.lower():
            log("请在浏览器中登录...")
            try:
                await page.wait_for_url("**/console/**", timeout=300000)
                await page.goto(ACTIVITY_URL, wait_until="domcontentloaded")
                await asyncio.sleep(3)
            except PwTimeout:
                pass

        # 保存登录状态
        await ctx.storage_state(path=str(STATE_PATH))
        log("登录状态已保存")

        # 同步时间
        offset_ms = await sync_time(page)

        goods = TEST_GOODS if test_mode else FLASH_SALE
        log(f"商品: {goods['name']}")

        # 等待前端 CSRF token 就绪
        for _ in range(50):
            if await page.evaluate("() => !!window.__latestCsrf"):
                break
            await asyncio.sleep(0.2)
        else:
            log("未捕获到 CSRF token，请求可能失败")
        csrf_len = await page.evaluate("() => (window.__latestCsrf || '').length")
        log(f"CSRF token 长度: {csrf_len}" + ("" if csrf_len else "（为空，下单请求大概率失败）"))

        if test_mode:
            log("测试模式: 直接调用 do-goods")
            success = await rapid_purchase(page, goods, duration=5)
            if success:
                log("测试成功，API 格式正确")
            else:
                log("测试完成。查看返回的 code 判断格式是否正确。")
            return

        # 等待抢购时间
        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = find_nearest_time(offset_ms)

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"等待 {wait_sec:.0f}s ({wait_sec/60:.1f}min)")
        log(f"提前量 {abs(ADVANCE_MS)}ms，会在目标时间前 {abs(ADVANCE_MS)/1000:.1f} 秒开始")

        warmed_up = False
        resynced = False
        while True:
            now_ms = int(time.time() * 1000) + offset_ms
            remaining = (target_dt.timestamp() * 1000 - now_ms) / 1000

            # 剩余 3 分钟时重新对时一次（Date 头实时对时，安全，修正长时间等待的微小漂移）
            if remaining <= 180 and not resynced:
                log("剩余3分钟，重新对时...")
                offset_ms = await sync_time(page)
                resynced = True
                continue  # 用新偏移重算 remaining

            # remaining 是正数表示还没到点，所以是 <= 3.0s 时开始（即提前3秒）
            if remaining * 1000 <= -ADVANCE_MS:
                log(f"提前 {abs(ADVANCE_MS)}ms 开始")
                break

            if remaining <= 10:
                log(f"{remaining:.1f}s")
            elif int(remaining) % 60 == 0:
                log(f"  还剩 {remaining/60:.0f}min")

            # 提前10秒发一个请求，把连接建好
            if remaining <= WARMUP_SECONDS and not warmed_up:
                await warmup(page, goods)
                warmed_up = True

            await asyncio.sleep(0.05 if remaining <= 5 else 1)

        # 执行抢购
        success = await rapid_purchase(page, goods)

        if success:
            log("抢购成功，请在浏览器中完成支付")
            await asyncio.sleep(600)
        else:
            log("未抢到。")

    except KeyboardInterrupt:
        log("用户中断")
    except Exception as e:
        log(f"错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            await ctx.storage_state(path=str(STATE_PATH))
        except:
            pass
        await browser.close()
        await pw.stop()


def main():
    parser = argparse.ArgumentParser(description="腾讯云服务器抢购终极方案")
    parser.add_argument("--time", type=str, help="HH:MM")
    parser.add_argument("--test", action="store_true", help="测试模式")
    parser.add_argument("--loop", action="store_true", help="持续运行")
    args = parser.parse_args()

    asyncio.run(run(target_time=args.time, test_mode=args.test, loop_mode=args.loop))


if __name__ == "__main__":
    main()
