// ===== 爬取統計 =====
// 內容:今日/歷史檢索筆數、爬取流量、執行時間,機器篩選,API 用量整合卡。
// 相依 API:/api/analytics、/api/monthly(取今日目標 per_workday)。
// 與「檢索進度」共用 monthlyData(定義在 progress.js):renderAnalytics 會順帶
// 更新今日進度小區塊(renderToday)與判斷今日達標(checkDailyGoal)。

// ----- 狀態 -----
let anData = null;            // 最近一次爬取統計資料(供勾選切換時即時重算,不必重抓)
const anSel = new Set();      // 已勾選的機器;空 = 全部合計
let anMachinesSig = "";       // 機器清單簽名,變了才重建勾選列(避免每 5 秒閃動)

// 由「資料變動」觸發的統計刷新節流:檢索進行中 summary 每批 flush 一次 → 資料簽章頻繁變動,
// 若每次都重抓 /api/analytics,撞上當月 Excel 剛被併入(cold)那次就要等較久。改成最短
// AN_MIN_INTERVAL_MS 才抓一次:間隔內的變動先記著,到時間補刷一次最新即可。
// 注意:只節流「資料變動」這條路;手動切到本頁、檢索結束(control.js)直接呼叫 tickAnalytics()
// 不走節流,故收尾與切頁一定拿到最新。
const AN_MIN_INTERVAL_MS = 12000;
let anLastFetch = 0;          // 上次實際發出 /api/analytics 的時刻(ms)
let anThrottleTimer = null;   // 節流中的待補刷計時器

// 蒐集資料中出現過的所有機器名稱(今日 + 歷史);「未標記」不列為篩選選項
function allMachines(d) {
  const s = new Set();
  const add = o => { if (o) for (const k of Object.keys(o)) s.add(k); };
  add(d.today.machines);
  for (const h of d.history) add(h.machines);
  s.delete("未標記");
  return [...s].sort();
}

// 依勾選回傳某筆資料的某欄位:沒勾 = 總計;有勾 = 勾選機器加總
function pickVal(entry, field) {
  if (!anSel.size) return entry[field] || 0;
  let s = 0;
  for (const name of anSel) {
    const m = entry.machines && entry.machines[name];
    if (m) s += m[field] || 0;
  }
  return s;
}

function toggleMachine(el) {
  if (el.checked) anSel.add(el.value); else anSel.delete(el.value);
  renderAnalytics();
}

function buildMachineFilter() {
  const box = document.getElementById("an-filter");
  const machines = allMachines(anData);
  // 只有一台(或沒有)就不顯示篩選列
  if (machines.length <= 1) { box.style.display = "none"; box.innerHTML = ""; anMachinesSig = ""; return; }
  for (const m of [...anSel]) if (!machines.includes(m)) anSel.delete(m);  // 清掉已消失的勾選
  const sig = machines.join("|");
  if (sig === anMachinesSig) return;   // 機器清單沒變就不重建(保留焦點、不閃)
  anMachinesSig = sig;
  box.style.display = "flex";
  let html = `<span>機器篩選(預設全部):</span>`;
  for (const m of machines) {
    const on = anSel.has(m) ? "checked" : "";
    html += `<label><input type="checkbox" value="${m}" ${on} onchange="toggleMachine(this)">${m}</label>`;
  }
  box.innerHTML = html;
}

function renderAnalytics() {
  if (!anData) return;
  const a = anData;
  document.getElementById("an-date").textContent = a.today.date;
  document.getElementById("an-q").textContent = pickVal(a.today, "queries").toLocaleString();
  document.getElementById("an-b").textContent = fmtBytes(pickVal(a.today, "bytes"));
  document.getElementById("an-t").textContent = fmtDur(pickVal(a.today, "elapsed_sec"));
  document.getElementById("an-m").textContent = fmtBytes(a.today.machine_used_bytes);
  const tb = document.getElementById("an-hist");
  tb.innerHTML = "";
  for (const d of a.history) {
    const sec = pickVal(d, "elapsed_sec");
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${d.date}</td>`
      + `<td class="num">${pickVal(d, "queries").toLocaleString()}</td>`
      + `<td class="num">${fmtBytes(pickVal(d, "bytes"))}</td>`
      + `<td class="num">${sec ? fmtDur(sec) : "—"}</td>`;
    tb.appendChild(tr);
  }
  if (!a.history.length) tb.innerHTML = `<tr><td colspan="4" class="hint">尚無紀錄</td></tr>`;

  renderApiUsage(a.api, a.quota, a.case_hourly);
  renderToday();    // 今日進度小區塊(定義在 progress.js):今日目標 vs 今日已完成
  checkDailyGoal();
}

// 整合卡:每支 userCode 一列。月查詢 / 當前時段輸出總額 / 本小時單筆案號最多次。
// 三來源都以 userCode 為主鍵(遮罩=前4碼+*,前後端一致),用遮罩字串合併。
function renderApiUsage(api, quota, caseHourly) {
  api = api || { codes: [], total: 0, data_date: "" };
  quota = quota || { codes: [], cap: 0, segment: "" };
  caseHourly = caseHourly || { codes: [], case_cap: 0, hour: "" };
  const box = document.getElementById("an-apicard");
  const seg = document.getElementById("q-seg");
  const cap = quota.cap || 0;
  const caseCap = caseHourly.case_cap || 0;

  const map = new Map();
  for (const c of api.codes) map.set(c.code, { code: c.code, month: c.count || 0 });
  for (const c of quota.codes) {
    const e = map.get(c.code) || { code: c.code, month: 0 };
    Object.assign(e, { machines: c.machines, used: c.est_used, remaining: c.remaining,
                       pct: c.pct, hasQuota: true });
    map.set(c.code, e);
  }
  for (const c of caseHourly.codes) {
    const e = map.get(c.code) || { code: c.code, month: 0 };
    e.caseMax = c.max || 0; e.caseNear = c.near; e.caseTop = c.top || [];
    map.set(c.code, e);
  }
  const rows = [...map.values()];
  if (!rows.length) {
    box.innerHTML = `<div class="hint">尚未設定 userCode,或找不到匯出檔與下載紀錄。</div>`;
    seg.textContent = "";
    return;
  }
  const segName = quota.segment === "busy" ? "上班" : "下班";
  seg.textContent = `${segName}時段`;
  // 「本時同號」說明改放每列標籤旁的資訊 icon(取代卡片底部那行小字)
  const caseTip = caseHourly.hour
    ? `本小時(${caseHourly.hour})同一案號被輸出最多的次數,達 ${caseCap} 會被擋;時段總額於 08:00/18:00 重置。`
    : `本小時同一案號被輸出最多的次數,達 ${caseCap} 會被擋;時段總額於 08:00/18:00 重置。`;
  box.innerHTML = "";
  for (const c of rows) {
    const pct = c.pct || 0;
    // 顏色一律走 token(--danger/--warn/--ok),深淺配色才會跟著切換。
    // 舊寫法 var(--stop,#e03131) 的 --stop 從來沒定義過,實際永遠吃後面的備援值。
    // color 同時用於數字文字與進度條填充 —— 文字取 ink(對比足),條用主色
    const color = pct >= 100 ? "var(--danger-ink)" : pct >= 80 ? "var(--warn-ink)" : "var(--ok-ink)";
    const barColor = pct >= 100 ? "var(--danger)" : pct >= 80 ? "var(--warn)" : "var(--ok)";
    const cMax = c.caseMax || 0;
    const cPct = caseCap ? Math.round(cMax / caseCap * 100) : 0;
    const cColor = cPct >= 100 ? "var(--danger-ink)" : cPct >= 80 ? "var(--warn-ink)" : "var(--ok-ink)";
    const row = document.createElement("div");
    row.style.cssText = "margin:10px 0;padding-bottom:9px;border-bottom:1px solid var(--line);";
    let html =
      `<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px;">`
      + `<b style="font-size:var(--fs-body);">${c.code}</b></div>`
      + `<div style="display:flex;gap:18px;flex-wrap:wrap;font-size:var(--fs-sm);color:var(--dim);margin-bottom:5px;">`
      + `<span>月 <b>${(c.month || 0).toLocaleString()}</b> / 60,000</span>`
      + (c.hasQuota ? `<span>時段 <b style="color:${color};">${(c.used || 0).toLocaleString()}</b> / ${cap.toLocaleString()}${pct >= 100 ? " ⚠" : ""}</span>` : "")
      + (cMax ? `<span>本時同號 <i class="info" tabindex="0" data-tip="${caseTip}"></i> <b style="color:${cColor};">${cMax}</b> / ${caseCap}${cPct >= 100 ? " ⚠" : ""}</span>` : "")
      + `</div>`;
    if (c.hasQuota) {
      html += `<div style="height:8px;border-radius:var(--r-pill);background:var(--track);overflow:hidden;">`
        + `<div style="height:100%;width:${Math.min(100, pct)}%;background:${barColor};"></div></div>`;
    }
    if (c.caseNear && c.caseTop && c.caseTop.length) {
      const tops = c.caseTop.map(x => `${x.patent}×${x.count}`).join("、");
      html += `<div style="font-size:var(--fs-hint);color:var(--warn-ink);margin-top:3px;">`
        + `⚠ 本小時這些案號快到每小時上限:${tops}</div>`;
    }
    row.innerHTML = html;
    box.appendChild(row);
  }
}

async function tickAnalytics() {
  // 這次實際發出請求 → 記下時刻並取消待補刷(它已被本次涵蓋),作為節流的基準點。
  anLastFetch = Date.now();
  if (anThrottleTimer) { clearTimeout(anThrottleTimer); anThrottleTimer = null; }
  // 首次載入(尚無資料)顯示大轉圈;之後每次定時刷新用標題旁的小轉圈,不蓋掉數字、不跳版面
  const loading = document.getElementById("an-loading");
  const spin = document.getElementById("an-spin");
  const first = !anData;
  if (first) loading.classList.add("on");
  else spin.style.display = "inline-block";
  try {
    anData = await (await fetch("/api/analytics")).json();
    await ensureMonthlyForToday();   // 取「今日目標」(per_workday)用;沒有就退化成只顯示已完成
    buildMachineFilter();
    renderAnalytics();
  } catch (e) {
  } finally {
    if (first) loading.classList.remove("on");
    else spin.style.display = "none";
  }
}

// 為了「今日進度」的目標值(per_workday)而取一次本月進度;已經有(自己或在檢索進度頁
// 算過)就直接用,不重抓。本月數字本身仍只在檢索進度頁按「更新計算」時才會變。
// monthlyData 定義在 progress.js,兩頁共用同一份。
async function ensureMonthlyForToday() {
  if (monthlyData) return;
  try { monthlyData = await (await fetch("/api/monthly")).json(); } catch (e) {}
}

function analysisOpen() {
  return isTabOpen("analysis");
}

// 偵測到檢索資料有變時呼叫(由 monitor.js 的 tickStatus 觸發):只處理「正開著」的本頁;
// 沒開就不動,切過去時 showTab 自然會抓最新。距上次抓已滿間隔 → 立即抓;未滿 → 排一次補刷
// (到「上次抓 + 間隔」時補抓一次最新);已排好就不重排。如此檢索高峰期最多每 AN_MIN_INTERVAL_MS 抓一次,
// 而最後一次變動仍會由補刷收斂到最新。
function onDataChanged() {
  if (!analysisOpen()) return;
  const wait = AN_MIN_INTERVAL_MS - (Date.now() - anLastFetch);
  if (wait <= 0) { tickAnalytics(); return; }
  if (anThrottleTimer) return;            // 已有待補刷,等它即可(別疊一堆計時器)
  anThrottleTimer = setTimeout(() => {
    anThrottleTimer = null;
    if (analysisOpen()) tickAnalytics();  // 補刷前再確認本頁仍開著
  }, wait);
}

// ===== 頁籤註冊 =====
// 每次切過來都刷一次(tickAnalytics 自己有 12 秒節流,切來切去不會連打)。
registerTab({
  id: "analysis",
  label: "爬取統計",
  order: 30,
  onShow() { tickAnalytics(); },
});
