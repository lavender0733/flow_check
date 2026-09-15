"""
GPSS Web (front-end) Batch Query  — 傳統檢索版
=============================================

走 TIPO GPSS 前端網頁批次檢索(用 Playwright 開分頁、填查詢框、按查詢、讀「共 N
筆」),不經官方 API。相對 API 版:流量大很多、需對抗前端的 session/IP 管制,但
不需要 API 驗證碼。適合「驗證前端是否查得到」或手上沒有 API 額度時使用。

本檔把傳統前端檢索對齊到與 gpss_api_query.py 相同的輸入/輸出慣例,
讓網頁控制台的兩種檢索方式共用同一套清單與統計:
  - 讀 patents.txt(與 API 版共用的檢索清單),不再寫死 PATENTS。
  - 輸出 output/summary_<時間>_<機器>.csv,欄位 idx,patent,status,hits,bytes,code
    (與 API 版一致,爬取統計才掃得到);code 固定為 "web"(不耗 API 額度,額度統計排除)。
  - 每跑完一輪追加一行到 data/machines/run_log_<機器>.jsonl(供網頁累計「執行時間」)。
  - 管制/降速時用 ##WARN## / ##CLEAR## 標記輸出(網頁警告橫幅吃這個),
    自動停止由環境變數 GPSS_LIMIT_DETECT 控制(網頁的「可用上限偵測」開關)。

用法(與 API 版相同參數,所以網頁控制台同一套即可驅動):
    python gpss_web_query.py            # patents.txt 全部
    python gpss_web_query.py 2000       # 跑 2000 筆(清單較短會循環)
    python gpss_web_query.py 2000 2000  # 跑第 2001-4000 筆(筆數 起始位置)

一次性安裝(僅傳統檢索需要):
    pip install playwright
    playwright install chromium
"""

import os
import re
import sys
import csv
import json
import time
import random
import asyncio
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from playwright.async_api import async_playwright

# 把 program/ 放上 sys.path，讓 core / gpss / common / yearly_stats 都能以套件匯入。
# 這是全專案唯一動 sys.path 的地方（每個進入點各一行），取代原本散在模組內的路徑魔法。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 機器代號與執行紀錄路徑與伺服器端共用同一份定義,不再各自重推。
# 執行紀錄:每跑完一輪追加一行 JSON,供網頁累計「執行時間」用。各機寫各機一份
# (run_log_<機器>.jsonl),網頁讀取時掃全部相加;同步時不會互相覆蓋。
# (筆數與流量另由 output/ 的 summary CSV 直接統計,故 CSV 才是筆數/流量的權威來源)。
from core.paths import MACHINE_ID, RUN_LOG_PATH   # noqa: E402  (sys.path 設定後才能匯入)

try:
    from gpss import output_store   # 把零散的 summary CSV 併成各機一個 Excel(一天一張工作表)
except Exception:
    output_store = None


def _skip_consolidate() -> bool:
    """由網頁伺服器啟動時(GPSS_SKIP_CONSOLIDATE=1)略過自己併檔 —— 那個 Excel 在
    Google Drive 同步夾裡,load+save 整份很慢,會害「印完 summary 仍卡著不結束、網頁
    任務不自動停」。伺服器改在任務結束後用背景執行緒併檔。獨立用命令列跑(沒設這個
    環境變數)時照常自己併,行為不變。"""
    return os.environ.get("GPSS_SKIP_CONSOLIDATE") == "1"


# 這個檔所在的 gpss 目錄(注意:core.paths.BASE 是專案根目錄,兩者不同)。
GPSS_DIR = Path(__file__).resolve().parent
PATENTS_PATH = GPSS_DIR / "patents.txt"
OUTPUT_DIR = GPSS_DIR / "output"


# ============================================================
# Settings(可調)
# ============================================================
# TIPO 有四台等價前端伺服器(gpss1..gpss4),每筆查詢隨機輪換,分散負載避免單台節流。
GPSS_APS = [1, 2, 3, 4]


def gpss_url() -> str:
    """隨機挑一台 GPSS AP 伺服器的前端網址。"""
    return f"https://tiponet.tipo.gov.tw/gpss{random.choice(GPSS_APS)}/gpsskmc/gpssbkm"


CONCURRENCY = 4               # 同時並行的分頁數(降速主旋鈕:6→3 約砍半瞬時負載)
DELAY_SECONDS = 1.0           # 每個分頁查完的禮貌停頓(秒)
HEADLESS = True               # True 隱藏瀏覽器(較快;由網頁控制台啟動時務必 True)
BLOCK_RESOURCES = True        # 封鎖圖片/字型/CSS/多媒體以省流量

RETRIES = 2                   # 每筆重試次數(含 UNKNOWN 重試)
RETRY_BACKOFF = 5             # 重試前退避秒數(× 第幾次)
ACTION_TIMEOUT = 15000        # 單一動作逾時(ms),被擋時快速失敗去重試

# GPSS 對單一 session(cookie)累積查詢到約 57 筆後會整個擋下;因此每 BATCH_SIZE 筆
# 就重建一個全新 context(新 cookie),門檻不會被觸發。
BATCH_SIZE = 40               # 每批筆數;每批換全新 session
BATCH_COOLDOWN = 5           # 批次之間暫停秒數(放慢整體節奏,讓 IP 速率窗口冷卻)
BROWSER_RESTART_BATCHES = 25  # 每 N 批整個瀏覽器重啟一次(長時間執行用)

# 管制偵測:即使換了新 session 仍連續大量失敗,研判已被 IP/每日層級管制。
# 由環境變數 GPSS_LIMIT_DETECT 控制(網頁「可用上限偵測」開關),未設時預設開啟。
ABORT_ON_BLOCK = os.environ.get("GPSS_LIMIT_DETECT", "1") == "1"
BLOCK_FAIL_RATIO = 0.8        # 一批中失敗比例 ≥ 此值即視為「異常批」
BLOCK_BAD_BATCHES = 3         # 連續幾個異常批就中止(2→3:多給恢復暫停一次機會)
# 偵測到異常批時,不立刻中止,先做一段「恢復暫停」讓 IP 速率窗口冷卻再續跑;
# 暫停長度隨連續異常數遞增(第 1 次 RECOVERY_PAUSE、第 2 次 ×2…)。IP 速率管制
# 多為時間窗口制、等一下會解;若是每日累計上限則暫停也救不回,屆時才真正中止。
RECOVERY_PAUSE = 90           # 異常批後的基礎恢復暫停秒數

# 封鎖的資源類型;保留 document/script/xhr/fetch,結果頁才渲染得出「共 N 筆」。
BLOCKED_TYPES = {"image", "font", "stylesheet", "media"}

# 主機白名單:只放行 TIPO 自己的網域,擋掉所有第三方。GPSS 前端會載入 Google
# Tag Manager / Analytics / doubleclick / cloudflareinsights 等追蹤腳本,它們以
# script/xhr/ping 型別進來、繞過上面的型別封鎖,卻是流量大頭(實測佔約 88%,單筆
# 從 ~530KB 降到 ~50KB)且對「查共幾筆」毫無用處。每筆查詢都重抓、也沒快取。
ALLOW_HOSTS = ("tiponet.tipo.gov.tw",)

SUMMARY_PATH = OUTPUT_DIR / f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{MACHINE_ID}.csv"


def load_patents() -> list:
    """讀 patents.txt(與 API 版共用);一行一筆,# 開頭為註解。"""
    if not PATENTS_PATH.exists():
        return []
    out = []
    for line in PATENTS_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _int_arg(value: str, fallback: int) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        print(f"參數 '{value}' 不是數字,改用 {fallback}")
        return fallback


def resolve_run_params(default: int) -> tuple:
    """決定 (count, offset)。優先吃命令列參數(count [offset]),否則互動詢問。
    offset 用來分段:`python gpss_web_query.py 2000 2000` 跑第 2001-4000 筆。"""
    if len(sys.argv) > 1:
        count = _int_arg(sys.argv[1], default) or default
        offset = _int_arg(sys.argv[2], 0) if len(sys.argv) > 2 else 0
        return count, offset
    try:
        raw = input(f"要查詢幾筆?(直接 Enter = {default}): ").strip()
    except EOFError:
        return default, 0
    count = _int_arg(raw, default) if raw else default
    return (count or default), 0


async def query_patent(context, search_text: str, idx: int, total: int) -> dict:
    """開分頁、送查詢、判讀命中筆數。回傳 {patent, status, hits, bytes, by_type}。不存檔。"""
    page = await context.new_page()
    label = f"[{idx:>5}/{total}] {search_text}"

    # 流量計:用 CDP(Chrome DevTools Protocol)量「實際下線位元組」。
    # 舊版只加 Content-Length,但後端動態回應多為 Transfer-Encoding: chunked、
    # 不帶該標頭,會被當成 0、嚴重低估(實測自報 28.6MB、真實近 1GB)。
    # loadingFinished.encodedDataLength = 該請求收到的編碼後總位元組(含標頭/壓縮/chunked)。
    counter = {"bytes": 0}
    by_type = {}            # resource_type -> bytes(看出是哪類內容在吃流量)
    req_type = {}           # requestId -> resource_type
    cache_ids = set()       # 從快取餵回(x-cache-hit)的 requestId → 不計入網路流量

    cdp = await context.new_cdp_session(page)
    await cdp.send("Network.enable")

    def on_response_received(e):
        try:
            req_type[e["requestId"]] = e.get("type", "Other")
            # 命中快取餵回的回應帶 x-cache-hit 標頭;這些沒走網路,計數時要排除。
            hdrs = (e.get("response") or {}).get("headers") or {}
            if hdrs.get("x-cache-hit") or hdrs.get("X-Cache-Hit"):
                cache_ids.add(e["requestId"])
        except Exception:
            pass

    def on_loading_finished(e):
        try:
            rid = e.get("requestId")
            t = req_type.pop(rid, "Other")
            if rid in cache_ids:        # 從記憶體餵回,未走網路 → 不計
                cache_ids.discard(rid)
                return
            n = int(e.get("encodedDataLength", 0) or 0)
            counter["bytes"] += n
            by_type[t] = by_type.get(t, 0) + n
        except Exception:
            pass

    cdp.on("Network.responseReceived", on_response_received)
    cdp.on("Network.loadingFinished", on_loading_finished)

    hits = 0
    try:
        await page.goto(gpss_url(), wait_until="domcontentloaded", timeout=ACTION_TIMEOUT)
        await page.wait_for_load_state("networkidle", timeout=ACTION_TIMEOUT)

        search_box = page.locator("input[type='text']").first
        await search_box.wait_for(state="visible", timeout=ACTION_TIMEOUT)
        await search_box.fill(search_text)

        await page.get_by_role("button", name="查詢").last.click()
        await page.wait_for_load_state("networkidle", timeout=ACTION_TIMEOUT)

        body = await page.inner_text("body", timeout=5000)
        if "查無資料" in body or "共 0 筆" in body:
            status = "NO_RESULT"
        elif "session" in body.lower() and "expired" in body.lower():
            status = "SESSION_EXPIRED"
        elif "共" in body and "筆" in body:
            m = re.search(r"共\s*(\d+)\s*筆", body)
            if m:
                hits = int(m.group(1))
                status = f"OK ({hits} hits)"
            else:
                status = "OK"
        else:
            status = "UNKNOWN"
    finally:
        await page.close()

    print(f"{label}  -> {status}  ({human_bytes(counter['bytes'])})")
    return {"patent": search_text, "status": status, "hits": hits,
            "bytes": counter["bytes"], "by_type": by_type}


# 靜態資源「行程內快取」:TIPO 後端對 JS 包送 no-cache,連同一 context 內都不快取,
# 導致每開一個分頁就把整包 JS 重抓一次(實測佔總流量約 95%)。這裡自己存一份:
# 同一 URL 第一次放行並抓回 body 存起來,之後所有查詢(跨批、跨 context)直接用
# 記憶體那份回應,不再連線。只快取靜態 script(含 session token 的 document/xhr 不碰)。
_ASSET_CACHE = {}                       # url -> {"status", "headers", "body"}
_CACHE_STAT = {"hits": 0, "served_bytes": 0,
               "seen": 0, "fetch_err": 0, "first_err": ""}
CACHEABLE_TYPES = {"script"}            # 需要時可再加 "stylesheet"/"font",但那些已被 BLOCKED_TYPES 擋掉


async def block_route(route):
    """中止重資源與第三方請求;靜態 script 走行程內快取;其餘放行。"""
    req = route.request
    if req.resource_type in BLOCKED_TYPES:
        await route.abort()
        return
    host = urlparse(req.url).netloc
    if not any(host == a or host.endswith("." + a) for a in ALLOW_HOSTS):
        await route.abort()           # 非 TIPO 網域(追蹤/分析腳本)→ 擋,省流量
        return

    if req.resource_type in CACHEABLE_TYPES and req.method == "GET":
        _CACHE_STAT["seen"] += 1
        cached = _ASSET_CACHE.get(req.url)
        if cached is not None:                       # 命中:用記憶體那份餵回,完全不連線
            _CACHE_STAT["hits"] += 1
            _CACHE_STAT["served_bytes"] += len(cached["body"])
            headers = dict(cached["headers"])
            headers["x-cache-hit"] = "1"             # 讓 CDP 計數器辨識並排除(未走網路)
            try:
                await route.fulfill(status=cached["status"],
                                    headers=headers,
                                    body=cached["body"])
                return
            except Exception as e:
                _CACHE_STAT["fetch_err"] += 1
                if not _CACHE_STAT["first_err"]:
                    _CACHE_STAT["first_err"] = f"fulfill {type(e).__name__}: {e}"
        # 未命中(或 fulfill 失敗):讓瀏覽器正常下載(CDP 才算得到真實壓縮流量),
        # 內容由下方 context 的 response 事件抓進快取,不重抓。

    await route.continue_()


async def cache_response(response):
    """把瀏覽器下載到的靜態 script 存進行程內快取(用 body(),不重抓、不走 Node)。"""
    try:
        req = response.request
        if req.resource_type not in CACHEABLE_TYPES or req.method != "GET":
            return
        url = req.url
        if url in _ASSET_CACHE or not response.ok:
            return
        body = await response.body()
        # body() 已解壓;若沿用原始 content-encoding/length 標頭,瀏覽器會再解壓一次而毀檔。
        # 移除這些與傳輸編碼相關的標頭,讓 fulfill 以解壓後的原樣內容供應。
        drop = {"content-encoding", "content-length", "transfer-encoding"}
        headers = {k: v for k, v in response.headers.items() if k.lower() not in drop}
        _ASSET_CACHE[url] = {"status": response.status,
                             "headers": headers,
                             "body": body}
    except Exception:
        pass


async def run(query_count: int, offset: int = 0):
    start_time = time.time()

    patents = load_patents()
    if not patents:
        print("⛔ patents.txt 沒有任何專利號。請在網頁控制台的『檢索清單』填入後儲存,再執行。")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if output_store is not None and not _skip_consolidate():   # 開跑前先把先前完成的 CSV 併進本機 Excel,保持資料夾乾淨
        try:
            output_store.consolidate(MACHINE_ID)
        except Exception:
            pass

    # 從 offset 起建構執行清單;count 大於清單長度就從頭循環。
    run_list = [patents[i % len(patents)] for i in range(offset, offset + query_count)]
    total = len(run_list)
    end_idx = offset + total

    print(f"GPSS 傳統(前端)檢索  |  {total} queries (items {offset + 1}-{end_idx})  |  本機:{MACHINE_ID}")
    print(f"Output: {SUMMARY_PATH}")
    print(f"並行: {CONCURRENCY}  |  停頓: {DELAY_SECONDS}s  |  省流量封鎖: {BLOCK_RESOURCES}  |  自動停止: {ABORT_ON_BLOCK}")
    print("-" * 60)

    # results[pos] 對應全域 idx = offset + pos + 1
    results = [None] * total
    sem = asyncio.Semaphore(CONCURRENCY)

    async def worker(context, idx: int, patent: str):
        async with sem:
            result = None
            for attempt in range(1, RETRIES + 1):
                try:
                    result = await query_patent(context, patent, idx, end_idx)
                except Exception as e:
                    if attempt < RETRIES:
                        backoff = RETRY_BACKOFF * attempt
                        print(f"[{idx}/{end_idx}] {patent}  retry {attempt}/{RETRIES - 1} "
                              f"in {backoff}s ({type(e).__name__})")
                        await asyncio.sleep(backoff)
                        continue
                    print(f"[{idx}/{end_idx}] {patent}  FAILED after {RETRIES}: {e}")
                    result = {"patent": patent, "status": f"EXCEPTION: {e}", "hits": 0, "bytes": 0}
                    break

                # 查詢有送出但判讀不出(多為渲染時機)→ 重試;最後一次保留 UNKNOWN。
                if result["status"] == "UNKNOWN" and attempt < RETRIES:
                    backoff = RETRY_BACKOFF * attempt
                    print(f"[{idx}/{end_idx}] {patent}  UNKNOWN, retry "
                          f"{attempt}/{RETRIES - 1} in {backoff}s")
                    await asyncio.sleep(backoff)
                    continue
                break

            results[idx - offset - 1] = result
            await asyncio.sleep(DELAY_SECONDS)

    indexed = [(offset + j + 1, run_list[j]) for j in range(total)]
    batches = [indexed[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]

    # 先開好 CSV,每批落地,中途中斷最多只損失最後一批。欄位與 API 版一致(含 code 欄)。
    # code 固定填 "web":傳統檢索不耗 API 驗證碼額度,額度統計據此把這些列排除。
    f = SUMMARY_PATH.open("w", encoding="utf-8-sig", newline="")
    w = csv.writer(f)
    w.writerow(["idx", "patent", "status", "hits", "bytes", "code"])
    f.flush()

    done = 0
    bad_streak = 0
    aborted = False
    warned = False        # 目前是否已對使用者發出管制警告(供 ##CLEAR## 收回)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        try:
            for bnum, batch in enumerate(batches, 1):
                if bnum > 1 and (bnum - 1) % BROWSER_RESTART_BATCHES == 0:
                    await browser.close()
                    browser = await p.chromium.launch(headless=HEADLESS)
                    print("--- browser recycled ---")

                print(f"--- batch {bnum}/{len(batches)} "
                      f"(idx {batch[0][0]}-{batch[-1][0]}) — fresh session ---")
                try:
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale="zh-TW",
                        # 有些機器在 SSL 檢查/攔截網路下,route.fetch() 走 Node 堆疊會因
                        # 「公司根憑證只裝在系統庫、Node 不認」而失敗(unable to verify the
                        # first certificate),導致靜態快取整個失效。略過憑證驗證讓 fetch 成功。
                        ignore_https_errors=True,
                    )
                    if BLOCK_RESOURCES:
                        await context.route("**/*", block_route)
                        # 瀏覽器下載到的靜態 script,即時抓進行程內快取供後續查詢餵回。
                        context.on("response", lambda r: asyncio.create_task(cache_response(r)))
                    await asyncio.gather(*(worker(context, idx, pat) for idx, pat in batch))
                    await context.close()
                except Exception as e:
                    print(f"--- batch {bnum} ERROR: {e} ---")

                # 寫這批(補齊批次級錯誤留下的空洞)。
                for idx, pat in batch:
                    pos = idx - offset - 1
                    if results[pos] is None:
                        results[pos] = {"patent": pat,
                                        "status": f"EXCEPTION: batch {bnum} failed",
                                        "hits": 0, "bytes": 0}
                    r = results[pos]
                    w.writerow([idx, r["patent"], r["status"], r.get("hits", 0), r.get("bytes", 0), "web"])
                f.flush()

                done += len(batch)
                elapsed = time.time() - start_time
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                print(f"--- progress {done}/{total}  "
                      f"elapsed {fmt_dur(elapsed)}  eta {fmt_dur(eta)} ---")

                # 管制偵測:一批大部分都失敗就算「異常批」。
                batch_fail = sum(1 for idx, _ in batch
                                 if results[idx - offset - 1]["status"].startswith("EXCEPTION"))
                if batch_fail / len(batch) >= BLOCK_FAIL_RATIO:
                    bad_streak += 1
                    msg = (f"批次 {bnum} 大量失敗({batch_fail}/{len(batch)}),疑似被管制 "
                           f"(連續 {bad_streak}/{BLOCK_BAD_BATCHES})。")
                    print(f"##WARN## {msg}", flush=True)
                    warned = True
                else:
                    if warned:
                        print("##CLEAR## 失敗率已回穩,恢復檢索。", flush=True)
                        warned = False
                    bad_streak = 0

                if ABORT_ON_BLOCK and bad_streak >= BLOCK_BAD_BATCHES:
                    print(f"##WARN## 連續 {bad_streak} 批大量失敗(已做過恢復暫停仍未回穩),"
                          f"研判被 TIPO 每日層級管制,停止執行"
                          f"(已完成 {done}/{total} 筆,結果已保存)。", flush=True)
                    aborted = True
                    break

                if bnum < len(batches):
                    if bad_streak > 0:
                        # 異常批 → 長暫停讓 IP 速率窗口冷卻,長度隨連續異常數遞增。
                        pause = RECOVERY_PAUSE * bad_streak
                        print(f"##WARN## 偵測到管制,恢復暫停 {pause}s 後續跑"
                              f"(第 {bad_streak} 次)…", flush=True)
                        await asyncio.sleep(pause)
                    else:
                        await asyncio.sleep(BATCH_COOLDOWN)
        finally:
            f.close()
            await browser.close()

    # 統計(CSV 已寫):成功 / 判讀不出(UNKNOWN) / 失敗(EXCEPTION/SESSION_EXPIRED)。
    processed = [r for r in results if r is not None]
    n = len(processed)
    total_bytes = sum(r.get("bytes", 0) for r in processed)
    elapsed = time.time() - start_time

    unknown = sum(1 for r in processed if r["status"] == "UNKNOWN")
    failed = sum(1 for r in processed
                 if r["status"].startswith("EXCEPTION") or r["status"] == "SESSION_EXPIRED")
    no_result = sum(1 for r in processed if r["status"] == "NO_RESULT")
    success = n - unknown - failed - no_result

    print("-" * 60)
    print(f"Summary: {SUMMARY_PATH}")
    if aborted:
        print(f"⛔ 已中止(疑似被管制)— 完成 {n} 筆")
    print(f"成功: {success}  |  查無: {no_result}  |  判讀不出(UNKNOWN): {unknown}  |  失敗: {failed}  (共 {n} 筆)")
    print(f"網路流量總計(實際下線位元組): {human_bytes(total_bytes)}  ({total_bytes:,} bytes)")
    if n:
        print(f"平均每筆: {human_bytes(total_bytes / n)}")
    if _CACHE_STAT["hits"]:
        print(f"靜態快取:命中 {_CACHE_STAT['hits']} 次,"
              f"從記憶體供應 {human_bytes(_CACHE_STAT['served_bytes'])}(未走網路)")
    # 診斷:僅在異常時印(沒命中卻有進分支、或有 fulfill 失敗),用來判斷快取為何失效。
    if _CACHE_STAT["fetch_err"] or (_CACHE_STAT["seen"] and not _CACHE_STAT["hits"]):
        dbg = (f"[cache debug] script GET 進分支 {_CACHE_STAT['seen']} 次,"
               f"唯一 URL {len(_ASSET_CACHE)} 個,命中 {_CACHE_STAT['hits']},"
               f"fulfill 失敗 {_CACHE_STAT['fetch_err']}")
        if _CACHE_STAT["first_err"]:
            dbg += f"(首錯:{_CACHE_STAT['first_err']})"
        print(dbg)

    # 各資源類型的流量分佈:看出到底是哪類內容在吃流量,據以調整封鎖規則。
    by_type_total = {}
    for r in processed:
        for t, b in (r.get("by_type") or {}).items():
            by_type_total[t] = by_type_total.get(t, 0) + b
    if by_type_total:
        parts = sorted(by_type_total.items(), key=lambda kv: kv[1], reverse=True)
        print("流量分佈:" + "  |  ".join(
            f"{t} {human_bytes(b)} ({b / total_bytes * 100:.0f}%)"
            for t, b in parts if total_bytes
        ))
    print(f"執行時間: {fmt_dur(elapsed)}  ({elapsed:.1f} 秒)")

    # 追加本次執行紀錄(供網頁累計執行時間;中止也會記錄目前進度)。
    try:
        now = datetime.now()
        rec = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "queries": n,
            "success": success + no_result,
            "no_result": no_result,
            "failed": failed,
            "bytes": total_bytes,
            "elapsed_sec": round(elapsed, 1),
            "method": "web",
            "machine": MACHINE_ID,
            "summary": SUMMARY_PATH.name,
        }
        RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RUN_LOG_PATH.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[warn] 無法寫入執行紀錄 run_log: {e}")

    if output_store is not None and not _skip_consolidate():   # 收尾:把本次 CSV 併進 Excel(被硬殺則由下次開跑前補併)
        try:
            n = output_store.consolidate(MACHINE_ID)
            if n:
                print(f"已把本次結果併入 output/summary_{MACHINE_ID}_<本月>.xlsx(當天工作表)。")
        except Exception:
            pass


def main():
    count, offset = resolve_run_params(default=len(load_patents()) or 1)
    asyncio.run(run(count, offset))


if __name__ == "__main__":
    main()
