// ===== 檢索控制台 =====
// 內容:檢索方式切換(API/傳統)、API 驗證碼池(切換/新增)、檢索清單編輯、
//       筆數、開始/停止、執行狀態與 log(tickJob)。
// 相依 API:/api/config、/api/keys、/api/patents、/api/run、/api/stop、/api/job。
// 註:loadConfig 也順手帶入 monitor.js 用到的 quota_gb 與 speed_floor_mbps 顯示預設值。

// ----- 狀態 -----
const mode = "count";         // 只有「指定筆數」一種模式(流量自動停已於 7.3.0 移除)
let method = "api";           // 檢索方式: api(官方 API) / web(傳統前端)
let apiNoteText = "API 驗證碼:讀取中…";   // 標題後小字(API 模式):目前用哪組驗證碼
let apiNoteColor = "var(--dim)";
let keys = [];                // 驗證碼池(只含遮罩碼)
let activeKey = null;         // 本機目前選用的驗證碼 id
let cfg = { max_count_per_run: 2000 };
let prevStatus = null;
let dismissedWarning = null;  // 使用者按掉的警告文字;同一則不再跳,警告解除或換新內容才重顯示
let jobElapsedBase = 0;       // 伺服器回報的執行秒數
let jobElapsedAt = 0;         // 收到上述秒數的當下(Date.now())
let jobRunning = false;       // 任務是否執行中(決定執行時間要不要每秒往上加)

// ===== 執行設定:檢索方式 =====
function setMethod(m) {
  method = m;
  document.getElementById("meth-api").classList.toggle("on", m === "api");
  document.getElementById("meth-web").classList.toggle("on", m === "web");
  document.getElementById("ctl-title").textContent =
    "GPSS檢索（" + (m === "api" ? "API" : "傳統前端") + "）";
  updateCtlNote();
}

// 標題後面那行小字:API 模式顯示驗證碼狀態(由 loadConfig 填)。
// 傳統檢索說明已改為「傳統檢索」頁籤後面的固定 info icon(見 control.html)。
function updateCtlNote() {
  const note = document.getElementById("ctl-note");          // 區塊內:API 驗證碼狀態
  const krow = document.getElementById("keysel-row");
  const isApi = (method === "api");
  if (krow) krow.style.display = isApi ? "" : "none";
  if (isApi) {
    if (note) { note.textContent = apiNoteText; note.style.color = apiNoteColor; }
  } else {
    const kf = document.getElementById("keyadd-form");
    if (kf) kf.style.display = "none";
  }
}

async function loadConfig() {
  try {
    cfg = await (await fetch("/api/config")).json();
    document.getElementById("quota").textContent = cfg.quota_gb;
    document.getElementById("maxcount").textContent = cfg.max_count_per_run;
    document.getElementById("count").max = cfg.max_count_per_run;
    // 速率下限預設值(使用者沒自訂過才採用 config 的值;speedFloor 在 monitor.js)
    if (!speedFloorUserSet && cfg.speed_floor_mbps != null) {
      speedFloor = cfg.speed_floor_mbps;
      document.getElementById("spfloor").value = speedFloor;
      renderPeak();
    }
    // 用量通知開關/門檻:以伺服器本機偏好為準(重開留得住);函式在 monitor.js
    if (typeof applyNotifyFromServer === "function") {
      applyNotifyFromServer(cfg.notify_enabled, cfg.notify_threshold);
    }
    // 流量暴增預警開關/門檻:同樣以伺服器本機偏好為準;函式在 monitor.js
    if (typeof applySurgeFromServer === "function") {
      applySurgeFromServer(cfg.surge_enabled, cfg.surge_min_mb);
    }
    // 排除內網流量開關:同樣以伺服器本機偏好為準;函式在 monitor.js
    if (typeof applyExcludeLanFromServer === "function") {
      applyExcludeLanFromServer(cfg.exclude_lan);
    }
    if (cfg.userCode_set) {
      apiNoteText = "API 驗證碼:已設定 ✓";
      apiNoteColor = "#2f9e44";
    } else {
      apiNoteText = "API 驗證碼:未設定 — 請編輯 gpss/api_config.json 填入 userCode";
      apiNoteColor = "var(--warn)";
    }
    updateCtlNote();
  } catch (e) {}
}

// ===== API 驗證碼池 =====
// 驗證碼下拉:列出驗證碼池(只顯示 label + 遮罩碼),標出本機目前選用的那組;
// 切換即寫回本機設定(api_config_<機器>.json 的 active_key)。新增驗證碼請編輯 api_keys.json。
async function loadKeys() {
  try {
    const d = await (await fetch("/api/keys")).json();
    keys = d.keys || [];
    activeKey = d.active || (keys[0] && keys[0].id) || null;
    const sel = document.getElementById("keysel");
    sel.innerHTML = "";
    if (!keys.length) {
      const o = document.createElement("option");
      o.textContent = "(驗證碼池是空的 — 請編輯 api_keys.json)";
      o.value = ""; sel.appendChild(o); sel.disabled = true;
      const codeEl = document.getElementById("ctl-code");
      if (codeEl) codeEl.textContent = "—";
      apiNoteText = "API 驗證碼:未設定 — 請在 gpss/api_keys.json 加一組";
      apiNoteColor = "var(--warn)";
      updateCtlNote();
      return;
    }
    sel.disabled = false;
    // 下拉只放名稱/備註(中文無襯線);遮罩碼另外用等寬字顯示在 #ctl-code,兩者字體分開
    for (const k of keys) {
      const o = document.createElement("option");
      o.value = k.id;
      o.textContent = `[${k.id}] ${k.label}`;
      if (k.id === activeKey) o.selected = true;
      sel.appendChild(o);
    }
    reflectActiveNote();
  } catch (e) {}
}

function reflectActiveNote() {
  const k = keys.find(x => x.id === activeKey);
  const codeEl = document.getElementById("ctl-code");
  if (k) {
    apiNoteText = "目前驗證碼";          // 字樣固定;是哪組由下拉顯示,遮罩碼放 #ctl-code
    apiNoteColor = "var(--txt)";        // 一般黑字,不用綠(綠在他處代表「正常/已啟用」,這裡只是狀態標籤)
    if (codeEl) codeEl.textContent = k.masked;            // 遮罩碼(等寬字)
  } else if (codeEl) {
    codeEl.textContent = "—";
  }
  updateCtlNote();
}

async function switchKey() {
  const sel = document.getElementById("keysel");
  const id = sel.value;
  const toast = document.getElementById("keytoast");
  if (!id) return;
  try {
    const d = await (await fetch("/api/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    })).json();
    if (d.ok) {
      activeKey = d.active;
      toast.textContent = "";          // 不再跳「已切換到」;下拉與「目前驗證碼」即時反映即為回饋
      reflectActiveNote();
    } else {
      toast.textContent = "切換失敗:" + (d.message || "");
      toast.style.color = "var(--warn)";
    }
  } catch (e) {
    toast.textContent = "切換失敗:" + e;
    toast.style.color = "var(--warn)";
  }
}

// 新增驗證碼:展開小表單,存進 api_keys.json。存完只回遮罩碼(前四碼 + *),不自動切換。
function toggleAddKey() {
  const f = document.getElementById("keyadd-form");
  const show = (f.style.display === "none" || !f.style.display);
  f.style.display = show ? "flex" : "none";
  if (show) {
    document.getElementById("keyadd-label").value = "";
    document.getElementById("keyadd-code").value = "";
    document.getElementById("keyadd-toast").textContent = "";
    document.getElementById("keyadd-label").focus();
  }
}

async function addKey() {
  const label = document.getElementById("keyadd-label").value.trim();
  const code = document.getElementById("keyadd-code").value.trim();
  const t = document.getElementById("keyadd-toast");
  if (!code) { t.textContent = "請輸入驗證碼"; t.style.color = "var(--warn)"; return; }
  try {
    const d = await (await fetch("/api/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "add", label, userCode: code }),
    })).json();
    if (d.ok) {
      document.getElementById("keyadd-code").value = "";   // 立刻清掉明碼
      document.getElementById("keyadd-label").value = "";
      await loadKeys();   // 重載下拉(新碼以遮罩顯示);不自動切換,目前選用維持不變
      toggleAddKey();     // 收起新增表單作為成功回饋;新碼已出現在下拉清單
    } else {
      t.textContent = "新增失敗:" + (d.message || "");
      t.style.color = "var(--warn)";
    }
  } catch (e) { t.textContent = "新增失敗:" + e; t.style.color = "var(--warn)"; }
}

// ===== 檢索清單 =====
// 載入目前清單:更新筆數(供主畫面顯示),並把內容填進編輯視窗的文字框備用。
async function loadPatents() {
  try {
    const d = await (await fetch("/api/patents")).json();
    document.getElementById("patents").value = d.text;
    document.getElementById("patcount").textContent = d.count;
  } catch (e) {}
}

// 開啟編輯視窗:先抓最新清單填入,再顯示視窗。
async function openPatentsEditor() {
  document.getElementById("patmodaltoast").textContent = "";
  await loadPatents();
  document.getElementById("patmodal").classList.add("show");
  setTimeout(() => document.getElementById("patents").focus(), 50);
}

// 取消:不存檔直接關閉(未存的修改即捨棄,下次開啟會重抓伺服器上的清單)。
function closePatentsEditor() {
  document.getElementById("patmodal").classList.remove("show");
}

async function savePatents() {
  const t = document.getElementById("patmodaltoast");
  const text = document.getElementById("patents").value;
  try {
    const d = await (await fetch("/api/patents", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    })).json();
    document.getElementById("patcount").textContent = d.count;
    if (d.ok) {
      const pt = document.getElementById("pattoast");
      pt.textContent = "✓ " + d.message;
      pt.style.color = "#2f9e44";
      closePatentsEditor();
    } else {
      t.textContent = "失敗:" + d.message;
      t.style.color = "var(--warn)";
    }
  } catch (e) { t.textContent = "儲存失敗:" + e; t.style.color = "var(--warn)"; }
}

// ===== 執行狀態 / log(每 2 秒輪詢)=====
// 點掉警告橫幅:記住目前這則,tickJob 之後同一則就不再跳出(警告解除或換新內容會重置)。
function dismissWarn() {
  const wb = document.getElementById("warnbanner");
  dismissedWarning = wb._warnText || null;
  wb.style.display = "none";
  // 同步通知伺服器清掉這則警告:它存在 JobRunner.warning 裡,只在前端隱藏的話
  // 一重整網頁(dismissedWarning 歸零)又會跳回來。清掉後重整/重開都不再出現。
  fetch("/api/clearwarn", { method: "POST" }).catch(() => {});
}

async function tickJob() {
  try {
    const j = await (await fetch("/api/job")).json();
    const wb = document.getElementById("warnbanner");
    if (j.warning) {
      if (j.warning === dismissedWarning) {
        wb.style.display = "none";          // 這則已被使用者關掉 → 保持隱藏
      } else {
        if (wb._warnText !== j.warning) {   // 只在內容變動時重寫,避免每 2 秒閃爍
          wb._warnText = j.warning;
          wb.textContent = "⚠ " + j.warning + "（點此關閉）";
        }
        wb.style.display = "block";
      }
    } else {
      dismissedWarning = null;              // 警告解除 → 重置,同則之後再出現會重新顯示
      wb._warnText = null;
      wb.style.display = "none";
    }
    const [label, cls] = STATUS_MAP[j.status] || [j.status, "idle"];
    const stateEl = document.getElementById("jobstate");
    stateEl.textContent = label;
    stateEl.className = "st-" + cls;
    document.getElementById("jobdot").className = "dot " + cls;
    document.getElementById("jobmethod").textContent = METHOD_MAP[j.method] || j.method || "—";
    document.getElementById("jobstart").textContent = j.started_at || "—";
    // 執行時間:記下伺服器回報值與收到時刻,讓 updateJobElapsed 在輪詢之間每秒往上加。
    jobElapsedBase = j.elapsed_sec || 0;
    jobElapsedAt = Date.now();
    jobRunning = j.running;
    updateJobElapsed();
    document.getElementById("go").disabled = j.running;
    document.getElementById("stop").disabled = !j.running;

    // toast follows real transitions (replaces the lingering「已開始」)
    const toast = document.getElementById("toast");
    if (prevStatus === "running" && j.status === "done") {
      toast.textContent = `✓ 執行完成（共送出約 ${j.est_done} 筆）`;
      toast.style.color = "#2f9e44";
    } else if (prevStatus === "running" && j.status === "stopped") {
      toast.textContent = "■ 已停止（停在批次邊界,結果已保存）";
      toast.style.color = "var(--warn)";
    } else if (j.status === "running") {
      toast.textContent = "";
    }
    // 檢索剛結束:若爬取統計頁正開著就立刻刷新一次(否則下一輪偵測到資料變動時也會自動更新)。
    if (prevStatus === "running" && (j.status === "done" || j.status === "stopped")) {
      if (typeof tickAnalytics === "function"
          && !document.getElementById("panel-analysis").classList.contains("hidden")) tickAnalytics();
    }
    prevStatus = j.status;
    const log = document.getElementById("log");
    if (j.lines && j.lines.length) {
      // 從 log 行算出進度,釘一行在最底(自動捲到底時永遠看得到)。
      // 兩種來源:逐筆 [idx/總數] 與 批次「--- progress done/總數 ---」。
      // 傳統檢索是並行的,逐筆行不照順序印出(最後一行可能是 [69/70]),故不能取「最後一筆」;
      // 進度只會遞增,改取所有相符行裡 done 最大者,才不會被亂序或被未含括號的 progress 行漏掉。
      let progDone = -1, progTotal = 0;
      for (const ln of j.lines) {
        const m = ln.match(/\[\s*(\d+)\s*\/\s*(\d+)\s*\]/) || ln.match(/progress\s+(\d+)\s*\/\s*(\d+)/);
        if (m) {
          const d = +m[1], t = +m[2];
          if (d > progDone) { progDone = d; progTotal = t; }
        }
      }
      let text = j.lines.join("\n");
      if (progDone >= 0) {
        const pct = progTotal > 0 ? Math.floor(progDone / progTotal * 100) : 0;
        text += `\n━━━ 進度 ${progDone} / ${progTotal}（${pct}%）━━━`;
      }
      // 只在內容真的變動時才重寫,避免每 2 秒重設捲動位置(尤其新增了水平捲軸)。
      if (text !== log._lastText) {
        const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
        log.textContent = text;
        log._lastText = text;
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
    }
  } catch (e) {}
}

// 執行時間顯示:跑的時候每秒往上加(以最近一次伺服器回報值為基準 + 經過的秒數),
// 結束後就停在伺服器定格的總時間。
function updateJobElapsed() {
  const el = document.getElementById("jobelapsed");
  if (!el) return;
  if (!jobElapsedBase && !jobRunning) { el.textContent = "—"; return; }
  let sec = jobElapsedBase;
  if (jobRunning) sec += (Date.now() - jobElapsedAt) / 1000;
  el.textContent = fmtDur(sec);
}

async function runJob() {
  const toast = document.getElementById("toast");
  toast.textContent = "";
  const payload = {
    mode, method,
    count: parseInt(document.getElementById("count").value, 10),
    limit_detect: document.getElementById("limitdetect").classList.contains("on"),
  };
  try {
    const r = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    toast.style.color = d.ok ? "var(--btn)" : "var(--warn)";
    toast.textContent = d.message;
    tickJob();
  } catch (e) { toast.textContent = "送出失敗: " + e; }
}

async function stopJob() {
  const toast = document.getElementById("toast");
  try {
    const d = await (await fetch("/api/stop", { method: "POST" })).json();
    toast.textContent = d.message;
    tickJob();
  } catch (e) { toast.textContent = "停止失敗: " + e; }
}
