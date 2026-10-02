// ===== 流量監控(可獨立抽出的分頁) =====
// 內容:今日流量錶 + 網卡明細(tickStatus)、用量通知、動態 favicon、
//       測速/限速判定、伺服器健康橫幅。
//
// 對外相依:
//   - /api/status:流量、網卡、健康 都來自這支。
//   - /api/config:速率下限預設值與本機偏好(loadMonitorConfig,開頁讀一次)。
//   - /api/speedtest:測速。

// ----- 狀態 -----
let notifyEnabled = false;    // 用量通知開關
let notifyThreshold = 80;     // 達幾 % 跳通知
let notifyHintTimer = 0;      // 門檻變更確認小字的還原計時器
let notifiedKey = "";         // 已通知過的「日期@門檻」鍵;避免同一天同門檻重複通知
let speedFloor = 5;           // 正常速率下限(Mbps);低於判定疑似被限速
let speedFloorUserSet = false;// 使用者是否已自訂下限(優先於 config 預設)
let lastPeakBps = 0;          // 最近一次 status 的近 N 分最高下載速率(bytes/s)
let lastPeakWinMin = 0;       // 被動觀測視窗(分鐘)
let surgeEnabled = false;     // 流量暴增預警開關
let surgeThreshold = 50;      // 單分鐘用量達幾 MB 起偵測
let surgeHintTimer = 0;       // 門檻變更確認小字的還原計時器
let surgeSeenSeq = -1;        // 已看過的最大事件流水號(避免同一波重複跳)
let surgeDismissedSeq = -1;   // 已按掉的事件流水號;超過才再跳橫幅
let surgeFirstRender = true;  // 首次渲染只記住現況、不為「開頁前就發生」的舊事件跳橫幅
let lastStatus = null;        // 最近一次 /api/status;供暴增紀錄立即切換收合狀態

// ===== 排除內網流量 =====
// 走內網(內網 IP)的傳輸不計入對外額度。開關以「伺服器本機偏好檔」為準(各機獨立、
// 跨重開留得住);localStorage 只當即時預填。實際扣除在後端取樣時完成,這裡只管開關與顯示。
// ★ 落在後端 count_ranges 白名單的網段不受影響,仍完整計入。
let excludeLan = false;

function applyExcludeLan(on) {
  excludeLan = on;
  document.getElementById("lanswitch").classList.toggle("on", on);
}

function saveExcludeLanPrefs() {
  try {
    fetch("/api/prefs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exclude_lan: excludeLan }),
    }).catch(() => {});
  } catch (e) {}
}

function toggleExcludeLan() {
  applyExcludeLan(!excludeLan);
  try { localStorage.setItem("excludeLan", excludeLan ? "1" : "0"); } catch (e) {}
  saveExcludeLanPrefs();   // 後端即時套用到執行中的 Monitor,不必重啟
}

// 由 loadMonitorConfig(/api/config)帶入伺服器本機偏好(權威);首次未存過就用目前狀態種一次。
function applyExcludeLanFromServer(enabled) {
  if (enabled == null) {
    saveExcludeLanPrefs();
    return;
  }
  applyExcludeLan(!!enabled);
}

function restoreExcludeLan() {
  let on;
  try { on = localStorage.getItem("excludeLan"); } catch (e) {}
  // 預設開(與後端 cfg.exclude_lan=True 一致):沒設過就視為開,首次會把「開」種回伺服器。
  // 伺服器回應後再以 applyExcludeLanFromServer 校正。
  applyExcludeLan(on !== "0");
}

// ===== 用量通知 =====
// 達到設定的已用比例(%)時跳一次桌面通知。設定以「伺服器本機偏好檔」為準
//(prefs_<機器>.json,跨重開/換瀏覽器都留得住);localStorage 只當即時預填與離線後備。
function applyNotify(on) {
  notifyEnabled = on;
  document.getElementById("notifyswitch").classList.toggle("on", on);
}

function setNotifyHint(msg, color) {
  const el = document.getElementById("notifyhint");
  el.textContent = msg || "";
  el.style.color = color || "var(--dim)";
}

function notifyHintText() {
  // (開)已表示啟用;只在桌面通知不可用時補一句小字
  return (("Notification" in window) && Notification.permission === "granted")
    ? "" : "(僅頁內提醒)";
}

// 把目前的開關與門檻寫回伺服器本機偏好檔(權威來源)。失敗不影響本次操作。
function saveNotifyPrefs() {
  try {
    fetch("/api/prefs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notify_enabled: notifyEnabled, notify_threshold: notifyThreshold }),
    }).catch(() => {});
  } catch (e) {}
}

function toggleNotify() {
  const on = !notifyEnabled;
  if (on) {
    applyNotify(true);
    try { localStorage.setItem("usageNotify", "1"); } catch (e) {}
    saveNotifyPrefs();
    setNotifyHint(notifyHintText(), "var(--ok-ink)");
    // 桌面通知能用就順便要權限(被封鎖/拒絕也沒關係,頁內提醒照常運作)
    if (("Notification" in window) && Notification.permission === "default") {
      Notification.requestPermission().then(() => {
        if (notifyEnabled) setNotifyHint(notifyHintText(), "var(--ok-ink)");
      });
    }
  } else {
    applyNotify(false);
    try { localStorage.setItem("usageNotify", "0"); } catch (e) {}
    saveNotifyPrefs();
    setNotifyHint("");
    // 關閉用量通知:把它的橫幅一起收掉,再重算紅點(暴增若仍有未看的提醒紅點會保留)。
    document.getElementById("notifybanner").style.display = "none";
    refreshAlertDot();
  }
}

// 還原 notifyhint 的小字到「平時該顯示的內容」(開:視桌面通知狀態補一句;關:清空)。
function baselineNotifyHint() {
  setNotifyHint(notifyEnabled ? notifyHintText() : "", notifyEnabled ? "var(--ok-ink)" : undefined);
}

function saveNotifyThreshold() {
  let v = parseInt(document.getElementById("notifythreshold").value, 10);
  if (!Number.isFinite(v)) v = 80;
  v = Math.min(100, Math.max(1, v));
  document.getElementById("notifythreshold").value = v;
  notifyThreshold = v;
  notifiedKey = "";   // 改門檻 → 重新評估(允許再次通知)
  try { localStorage.setItem("usageNotifyThreshold", String(v)); } catch (e) {}
  saveNotifyPrefs();
  // 文字說明:確認剛變更的門檻 %(短暫顯示後回到平時的小字)。
  setNotifyHint(`✓ 已更新:用量達 ${v}% 時提醒`, "var(--ok-ink)");
  clearTimeout(notifyHintTimer);
  notifyHintTimer = setTimeout(baselineNotifyHint, 4000);
}

// 由 loadMonitorConfig(/api/config)帶入伺服器本機偏好,覆寫 localStorage 預填值(伺服器為準)。
// 伺服器尚未存過(回 null)時:沿用目前狀態並回寫一次,把舊的 localStorage 設定遷移上去。
function applyNotifyFromServer(enabled, threshold) {
  if (threshold != null) {
    notifyThreshold = Math.min(100, Math.max(1, parseInt(threshold, 10) || 80));
    document.getElementById("notifythreshold").value = notifyThreshold;
  }
  if (enabled == null) {
    saveNotifyPrefs();   // 首次:把目前(localStorage 還原的)狀態種到伺服器
    return;
  }
  applyNotify(!!enabled);
  setNotifyHint(enabled ? notifyHintText() : "", enabled ? "var(--ok-ink)" : undefined);
}

function restoreNotify() {
  let on, th;
  try {
    on = localStorage.getItem("usageNotify");
    th = localStorage.getItem("usageNotifyThreshold");
  } catch (e) {}
  if (th != null) {
    notifyThreshold = Math.min(100, Math.max(1, parseInt(th, 10) || 80));
    document.getElementById("notifythreshold").value = notifyThreshold;
  }
  // 頁內提醒不需通知權限,故只看先前是否開啟即可還原
  if (on === "1") {
    applyNotify(true);
    setNotifyHint(notifyHintText(), "var(--ok-ink)");
  } else {
    applyNotify(false);
  }
}

// 由 tickStatus 每次更新時呼叫:達門檻就提醒一次。
// 以「日期@門檻」為鍵,同一天同門檻只提醒一次;跨日(date 變)或掉回門檻下方會自動重置。
function checkUsageNotify(percent, date) {
  if (!notifyEnabled) return;
  const key = date + "@" + notifyThreshold;
  if (percent >= notifyThreshold) {
    if (notifiedKey === key) return;   // 今天這個門檻已提醒過
    notifiedKey = key;
    fireUsageAlert(percent);
  } else if (notifiedKey === key) {
    notifiedKey = "";   // 掉回門檻下方 → 允許之後再次提醒
  }
}

// 實際送出提醒:頁內橫幅(持續顯示) + alert 彈窗(最可靠,不受桌面通知封鎖影響);
// 若桌面通知剛好可用且已授權,順便也發一則。
function fireUsageAlert(percent) {
  const msg = `今日網路用量已達 ${percent.toFixed(1)}%,超過門檻 ${notifyThreshold}%。`;
  const b = document.getElementById("notifybanner");
  if (b) { b.textContent = "⚠ " + msg + "（點此關閉）"; b.style.display = "block"; }
  updateFavicon(true);   // 亮分頁紅點
  try {
    if (("Notification" in window) && Notification.permission === "granted") {
      new Notification("流量用量提醒", { body: msg, tag: "usage-quota" });
    }
  } catch (e) {}
  // 讓橫幅與紅點先繪出再跳 alert(alert 會阻塞渲染)。
  // 紅點不在這裡熄 —— 改由「點掉紅橫幅」清除(橫幅一定顯示,比依賴 alert 可靠)。
  setTimeout(() => { try { alert("⚠ 流量用量提醒\n\n" + msg); } catch (e) {} }, 60);
}

// ===== 流量暴增預警 =====
// 後端每次取樣判斷「該分鐘用量是否暴增」(超過門檻且達近期中位數數倍),暴增時歸因到
// 「當下 io_counters 增量最多、且有 inet 連線」的程式。設定同用量通知:以伺服器本機偏好為準。
function escHtml(s) {
  return String(s == null ? "" : s).replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
}

function applySurge(on) {
  surgeEnabled = on;
  document.getElementById("surgeswitch").classList.toggle("on", on);
}

function setSurgeHint(msg, color) {
  const el = document.getElementById("surgehint");
  el.textContent = msg || "";
  el.style.color = color || "var(--dim)";
}

function saveSurgePrefs() {
  try {
    fetch("/api/prefs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ surge_enabled: surgeEnabled, surge_min_mb: surgeThreshold }),
    }).catch(() => {});
  } catch (e) {}
}

function toggleSurge() {
  const on = !surgeEnabled;
  applySurge(on);
  try { localStorage.setItem("surgeEnabled", on ? "1" : "0"); } catch (e) {}
  saveSurgePrefs();
  if (on) {
    // 桌面通知能用就順便要權限(被拒也沒關係,頁內橫幅照常)
    if (("Notification" in window) && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  } else {
    document.getElementById("surgebanner").style.display = "none";
    refreshAlertDot();   // 關閉暴增預警:收掉橫幅後重算紅點(用量通知若還有未看的會保留)
  }
}

function saveSurgeThreshold() {
  let v = parseInt(document.getElementById("surgethreshold").value, 10);
  if (!Number.isFinite(v) || v < 1) v = 50;
  document.getElementById("surgethreshold").value = v;
  surgeThreshold = v;
  try { localStorage.setItem("surgeThreshold", String(v)); } catch (e) {}
  saveSurgePrefs();   // 後端會即時套到執行中的 Monitor,不必重啟
  setSurgeHint(`✓ 已更新:單分鐘達 ${v}MB 起偵測`, "var(--ok-ink)");
  clearTimeout(surgeHintTimer);
  surgeHintTimer = setTimeout(() => setSurgeHint(""), 4000);
}

// 由 loadMonitorConfig(/api/config)帶入伺服器本機偏好(權威);首次未存過就用目前狀態種一次。
function applySurgeFromServer(enabled, threshold) {
  if (threshold != null) {
    surgeThreshold = Math.max(1, parseInt(threshold, 10) || 50);
    document.getElementById("surgethreshold").value = surgeThreshold;
  }
  if (enabled == null) {
    saveSurgePrefs();
    return;
  }
  applySurge(!!enabled);
}

function restoreSurge() {
  let on, th;
  try {
    on = localStorage.getItem("surgeEnabled");
    th = localStorage.getItem("surgeThreshold");
  } catch (e) {}
  if (th != null) {
    surgeThreshold = Math.max(1, parseInt(th, 10) || 50);
    document.getElementById("surgethreshold").value = surgeThreshold;
  }
  applySurge(on === "1");
}

function dismissSurge() {
  surgeDismissedSeq = surgeSeenSeq;
  document.getElementById("surgebanner").style.display = "none";
  refreshAlertDot();   // 按掉暴增橫幅 → 重算紅點(這正是原本紅點按不掉的修正點)
}

// 由 tickStatus 每次帶 status 進來:對「本次連線期間新發生」的暴增跳橫幅。
// 暴增清單本身已合併進「用量明細」表(見 renderCalcLog),這裡只負責新事件的橫幅提醒。
function renderSurges(s) {
  const surges = s.surges || [];           // 新到舊(後端已反轉)
  const latest = surges.length ? surges[0] : null;
  // 首次渲染(本連線期間第一次拿到 status)只記住現況、不為「開頁前就發生」的舊事件跳橫幅;
  // 一定要在這次就清掉旗標,否則「開頁時無事件、稍後才發生第一筆」會被誤當舊事件而漏跳。
  if (surgeFirstRender) {
    surgeFirstRender = false;
    if (latest) surgeSeenSeq = surgeDismissedSeq = latest.seq;
    return;
  }
  if (latest && latest.seq > surgeSeenSeq) {
    surgeSeenSeq = latest.seq;
    if (surgeEnabled && latest.seq > surgeDismissedSeq) fireSurgeAlert(latest);
  }
}

// ===== 整合面板(網卡 + 用量明細 + 暴增紀錄):收合/展開 + 頁籤切換 =====
// 網卡、用量明細與暴增紀錄合併成同一張可收合面板,以子頁籤切換。狀態都記在 localStorage:
//   monitorPanelOpen — 面板收合/展開(預設展開,網卡是平時要看的)
//   monitorTab       — 目前停在哪個頁籤(nics / apps / calclog / surges;預設 nics)

const MONITOR_TABS = ["nics", "apps", "calclog", "surges"];

// 收合/展開(由標頭的 caret 觸發)。
function applyCalcLogCollapse(open) {
  const box = document.getElementById("detailbox");
  if (box) box.classList.toggle("open", open);
}
function toggleCalcLog() {
  const box = document.getElementById("detailbox");
  if (!box) return;
  const open = !box.classList.contains("open");
  applyCalcLogCollapse(open);
  try { localStorage.setItem("monitorPanelOpen", open ? "1" : "0"); } catch (e) {}
}
function restoreCalcLogCollapse() {
  let v;
  try { v = localStorage.getItem("monitorPanelOpen"); } catch (e) {}
  applyCalcLogCollapse(v !== "0");   // 預設展開
}

// 頁籤切換(網卡 / 今日程式排行 / 用量明細 / 暴增紀錄)。使用者點頁籤時若面板正收合著就順手展開;
// 還原狀態(fromUser 省略)則只切頁籤、不動收合狀態,以免覆寫使用者的收合偏好。
function switchMonitorTab(which, fromUser) {
  if (!MONITOR_TABS.includes(which)) which = "nics";
  const box = document.getElementById("detailbox");
  if (fromUser && box && !box.classList.contains("open")) {
    applyCalcLogCollapse(true);
    try { localStorage.setItem("monitorPanelOpen", "1"); } catch (e) {}
  }
  const map = {
    nics: ["tab-nics", "pane-nics"],
    apps: ["tab-apps", "pane-apps"],
    calclog: ["tab-calclog", "pane-calclog"],
    surges: ["tab-surges", "pane-surges"],
  };
  for (const k in map) {
    const [tabId, paneId] = map[k];
    const on = k === which;
    const tab = document.getElementById(tabId);
    const pane = document.getElementById(paneId);
    if (tab) tab.classList.toggle("on", on);
    if (pane) pane.style.display = on ? "block" : "none";
  }
  const tools = document.getElementById("calclog-tools");
  if (tools) tools.style.display = which === "calclog" ? "" : "none";
  try { localStorage.setItem("monitorTab", which); } catch (e) {}
  if (which === "calclog") loadCalcLog(false);   // 切進來才去拉明細;整天的量不隨每 5 秒輪詢送
}
function restoreMonitorTab() {
  let v;
  try { v = localStorage.getItem("monitorTab"); } catch (e) {}
  switchMonitorTab(MONITOR_TABS.includes(v) ? v : "nics");
}

// ===== 用量明細(逐分鐘,伺服器端記錄)=====
// 後端每次 NIC 結算(預設 60 秒)記一筆並寫進本機明細檔:網卡下載總量、其中判為內網(不計費)
// 的部分、真正算進額度的下載/上傳。暴增事件另看「暴增紀錄」頁籤。
// 以前明細只放在記憶體、只看近 5 分鐘,網頁沒開著的時段就什麼都查不到;現在整天都在、重開監控
// 也接得回來,可切換日期回頭查(額度用完的隔天還能查前一天)。整天的量不隨每 5 秒的輪詢送:切到這個頁籤、換日期,
// 或輪詢看到後端多了新的一筆(log_last 變了)時,才向 /api/traffic/log 拉。
let calcLogRows = [];          // 目前顯示那天的明細(舊到新)
let calcLogDate = null;        // 目前顯示的日期;null = 還沒載過
let calcLogToday = null;       // 後端的「今天」
let calcLogSeenLast = null;    // 已載入的最後一筆時間(今天);輪詢的 log_last 變了才重拉
let calcLogLoading = false;

const APP_LABELS = { __unattributed__: "未歸因(UDP/QUIC・標頭)", __offline__: "監控未執行期間" };
function appLabel(name) { return APP_LABELS[name] || name || "未知程式"; }
// 「未歸因」不是一支程式,名稱後面接一個 ⓘ 說明它是什麼(今日程式排行、暴增紀錄共用)。
const UNATTR_TIP = "網卡確實計到、但對不到任何 TCP 連線的量,多半是 UDP/QUIC(Chrome、YouTube、視訊會議)與封包標頭;"
  + "監控沒以系統管理員身分啟動時,所有流量都會落在這裡。";
// 這顆 ⓘ 在可捲動的表格裡,一般 .info 的提示框(CSS ::after)會被捲動區塊裁掉;
// 改成 .info-float:提示框是掛在 body 上的一個浮層,用 position:fixed 定位在 icon 下方。
function unattrInfo(name) {
  return name === "__unattributed__"
    ? `<i class="info info-float" tabindex="0" data-tip="${UNATTR_TIP}"></i>` : "";
}
(function () {
  let pop = null;
  const show = el => {
    if (!pop) {
      pop = document.createElement("div");
      pop.className = "info-pop";
      document.body.appendChild(pop);
    }
    pop.textContent = el.dataset.tip;
    pop.style.display = "block";
    const r = el.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - w - 8);
    let top = r.bottom + 8;
    if (top + h > window.innerHeight - 8) top = r.top - h - 8;   // 下方放不下就改放上方
    pop.style.left = Math.max(8, left) + "px";
    pop.style.top = Math.max(8, top) + "px";
  };
  const hide = () => { if (pop) pop.style.display = "none"; };
  // 表格每 5 秒重畫,icon 會換新的;用事件委派,不必每次重新綁。
  document.addEventListener("mouseover", e => {
    const el = e.target.closest && e.target.closest(".info-float");
    if (el) show(el);
  });
  document.addEventListener("mouseout", e => {
    if (e.target.closest && e.target.closest(".info-float")) hide();
  });
  document.addEventListener("focusin", e => { if (e.target.matches(".info-float")) show(e.target); });
  document.addEventListener("focusout", e => { if (e.target.matches(".info-float")) hide(); });
  window.addEventListener("scroll", hide, true);
})();
// 程序名稱欄(exe 檔名,可對照工作管理員)。「未歸因」「監控未執行期間」不是程式,沒有檔名。
function exeCell(exe) {
  return exe ? escHtml(exe) : "—";
}

async function loadCalcLog(fromPicker) {
  if (calcLogLoading) return;
  const sel = document.getElementById("calclog-date");
  const want = fromPicker && sel && sel.value ? sel.value : (calcLogDate || "");
  calcLogLoading = true;
  try {
    const d = await (await fetch("/api/traffic/log" + (want ? "?date=" + encodeURIComponent(want) : ""))).json();
    calcLogRows = d.rows || [];
    calcLogDate = d.date;
    calcLogToday = (d.dates && d.dates[0]) || d.date;
    if (d.date === calcLogToday) {
      calcLogSeenLast = calcLogRows.length ? calcLogRows[calcLogRows.length - 1].t : null;
    }
    if (sel) {
      const opts = (d.dates || [d.date]).map(x =>
        `<option value="${x}"${x === d.date ? " selected" : ""}>${x === calcLogToday ? x + "(今天)" : x}</option>`);
      sel.innerHTML = opts.join("");
      sel.value = d.date;
      if (sel.refreshPicker) sel.refreshPicker();
    }
    renderCalcLog();
  } catch (e) {
    // 拉不到就保留舊畫面;tickStatus 的連線橫幅會另外提醒後端斷線
  } finally {
    calcLogLoading = false;
  }
}

// 由 tickStatus 呼叫:只在「正看著今天的明細」且後端多了新的一筆時才重拉。
function maybeRefreshCalcLog(s) {
  const pane = document.getElementById("pane-calclog");
  if (!pane || pane.style.display === "none") return;
  if (calcLogDate !== null && calcLogDate !== s.date) return;   // 在看別天,不動
  if (s.log_last && s.log_last !== calcLogSeenLast) loadCalcLog(false);
}

function renderCalcLog() {
  const tb = document.getElementById("calclogbody");
  if (!tb) return;
  const onlyBusy = !!(document.getElementById("calclog-busy") || {}).checked;

  tb.innerHTML = "";
  for (let i = calcLogRows.length - 1; i >= 0; i--) {   // 新到舊
    const e = calcLogRows[i];
    if (onlyBusy && (e.cnt_recv || 0) + (e.cnt_sent || 0) < 1048576) continue;
    const tr = document.createElement("tr");
    const exc = e.excl_recv > 0 ? fmtBytes(e.excl_recv) : "—";
    tr.innerHTML =
        `<td>${e.t}</td>`
      + `<td class="num">${fmtBytes(e.nic_recv)}</td>`
      + `<td class="num">${exc}</td>`
      + `<td class="num">${fmtBytes(e.cnt_recv)}</td>`
      + `<td class="num">${fmtBytes(e.cnt_sent)}</td>`;
    tb.appendChild(tr);
  }
  if (!tb.children.length) {
    tb.innerHTML = `<tr><td colspan="5" class="empty-row">${calcLogRows.length ? "這天沒有 ≥1 MB 的分鐘。" : "這天還沒有明細。"}</td></tr>`;
  }
}

// ===== 今日程式排行 =====
// 後端從早上累計到現在的各程式用量(計入額度的量,已扣內網),含「未歸因」與「監控未執行期間」。
// 暴增預警只抓得到「某一分鐘突然變大」;整天細水長流的背景流量(雲端同步、備份、系統更新)
// 從來不會觸發暴增,卻一樣會把額度吃完 —— 這張表才看得出誰累積最多。
function renderApps(s) {
  const tb = document.getElementById("appsbody");
  if (!tb) return;
  const apps = s.day_apps || [];
  // 分母取「今日計入量」與「排行加總」較大者:升級當天排行從升級那刻才起算,加總會小於今日量;
  // 反過來也不讓占比超過 100%。
  const total = Math.max(s.used_bytes || 0, apps.reduce((t, a) => t + a.recv + a.sent, 0));
  if (!apps.length) {
    tb.innerHTML = `<tr><td colspan="6" class="empty-row">今天還沒有累計資料(每分鐘結算一次)。</td></tr>`;
    return;
  }
  const html = [];
  let shown = 0, rest = [0, 0];
  for (const a of apps) {
    const sum = a.recv + a.sent;
    // 小於 1MB 的併成一列,不讓一長串零碎程式把表撐爆
    if (shown >= 25 || (sum < 1048576 && !APP_LABELS[a.name])) { rest[0] += a.recv; rest[1] += a.sent; continue; }
    shown++;
    const pct = total > 0 ? (sum / total * 100) : 0;
    const special = APP_LABELS[a.name] ? " dim" : "";
    const label = escHtml(appLabel(a.name));
    const tip = unattrInfo(a.name);
    html.push(`<tr><td class="${special}" title="${tip ? "" : label}">${label}${tip}</td>`
      + `<td class="${special}" title="${escHtml(a.exe || "")}">${exeCell(a.exe)}</td>`
      + `<td class="num">${fmtBytes(a.recv)}</td><td class="num">${fmtBytes(a.sent)}</td>`
      + `<td class="num"><b>${fmtBytes(sum)}</b></td>`
      + `<td class="num"><span class="pctbar" style="--p:${Math.min(100, pct).toFixed(1)}%"></span>${pct.toFixed(1)}%</td></tr>`);
  }
  if (rest[0] + rest[1] > 0) {
    const sum = rest[0] + rest[1];
    html.push(`<tr><td class="dim">其他(零碎)</td><td class="dim">—</td><td class="num">${fmtBytes(rest[0])}</td>`
      + `<td class="num">${fmtBytes(rest[1])}</td><td class="num">${fmtBytes(sum)}</td>`
      + `<td class="num">${total > 0 ? (sum / total * 100).toFixed(1) : "0.0"}%</td></tr>`);
  }
  tb.innerHTML = html.join("");
}

// 當日暴增紀錄:先依「應用程式 + 發生時間」收合、加總流量;需要時才展開查看每筆 IP／PID 明細。
// 輪詢每 5 秒都會重畫表格,故把已展開的群組鍵留在這裡,不讓畫面更新後自動收合。
const expandedSurgeGroups = new Set();

function surgeGroupKey(name, time) {
  return JSON.stringify([name, time]);
}

function toggleSurgeGroup(key) {
  if (expandedSurgeGroups.has(key)) expandedSurgeGroups.delete(key);
  else expandedSurgeGroups.add(key);
  // 不必等下一次輪詢,直接用最近一次狀態重繪這張表。
  if (lastStatus) renderSurgeLog(lastStatus);
}

function renderSurgeLog(s) {
  const sc = document.getElementById("surgecount");
  if (sc) {
    const n = (s.surges || []).length;
    sc.textContent = n > 0 ? `・🔺 ${n}` : "";
    sc.style.color = n > 0 ? "var(--warn)" : "";
  }
  const tb = document.getElementById("surgelogbody");
  if (!tb) return;
  tb.innerHTML = "";
  const groups = new Map();
  const surges = s.surges || [];   // 新到舊(後端已反轉)

  for (const ev of surges) {
    const time = String(ev.time || "—");
    const top = Array.isArray(ev.top) && ev.top.length
      ? ev.top
      : [{ ip: "—", name: "未能歸因", pid: null, bytes: ev.bytes || 0 }];
    for (const source of top) {
      const raw = String(source.name || "");
      const name = appLabel(raw);
      const key = surgeGroupKey(name, time);
      let group = groups.get(key);
      if (!group) {
        group = { key, name, raw, exe: String(source.exe || ""), time, bytes: 0, details: [] };
        groups.set(key, group);
      }
      const bytes = Math.max(0, Number(source.bytes) || 0);
      group.bytes += bytes;
      group.details.push({
        ip: String(source.ip || "—"),
        pid: source.pid == null ? "—" : String(source.pid),
        exe: String(source.exe || ""),
        bytes,
      });
    }
  }

  // 清掉已不在今天資料中的展開鍵,避免集合隨著事件過期而一直累積。
  for (const key of expandedSurgeGroups) {
    if (!groups.has(key)) expandedSurgeGroups.delete(key);
  }

  if (!groups.size) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6" class="empty-row">今日尚未偵測到流量暴增。</td>`;
    tb.appendChild(tr);
    return;
  }

  for (const group of groups.values()) {
    const expanded = expandedSurgeGroups.has(group.key);
    const tr = document.createElement("tr");
    tr.className = "surge-group-row" + (expanded ? " open" : "");
    tr.innerHTML = `<td class="surge-expand-col"><button type="button" class="surge-expand" `
      + `aria-expanded="${expanded}" title="${expanded ? "收合" : "展開"}明細">▸</button></td>`
      + `<td>${escHtml(group.name)}${unattrInfo(group.raw)}</td>`
      + `<td>${exeCell(group.exe)}</td>`
      + `<td>${escHtml(group.time)}</td>`
      + `<td class="num">${group.details.length}</td>`
      + `<td class="num">${fmtBytes(group.bytes)}</td>`;
    tr.querySelector(".surge-expand").addEventListener("click", () => toggleSurgeGroup(group.key));
    tb.appendChild(tr);

    if (expanded) {
      const detailTr = document.createElement("tr");
      detailTr.className = "surge-detail-row";
      const detailRows = group.details.map(detail =>
        `<tr><td>${escHtml(detail.ip)}</td>`
        + `<td class="num">${escHtml(detail.pid)}</td><td class="num">${fmtBytes(detail.bytes)}</td></tr>`
      ).join("");
      detailTr.innerHTML = `<td colspan="6"><div class="surge-detail-wrap"><table class="surge-detail-table">`
        + `<thead><tr><th>IP</th><th class="num">PID</th><th class="num">使用流量</th></tr></thead>`
        + `<tbody>${detailRows}</tbody></table></div></td>`;
      tb.appendChild(detailTr);
    }
  }
}

function fireSurgeAlert(ev) {
  const top = (ev.top && ev.top[0]) ? appLabel(ev.top[0].name) : "未知來源";
  const msg = `${ev.time} 流量暴增:該分鐘用 ${fmtBytes(ev.bytes)}`
            + `(平時約 ${fmtBytes(ev.baseline)}),最可能來源:${top}。`;
  const b = document.getElementById("surgebanner");
  if (b) { b.textContent = "⚠ " + msg + "(點此關閉)"; b.style.display = "block"; }
  updateFavicon(true);   // 共用分頁紅點 =「還沒看的提醒」
  try {
    if (("Notification" in window) && Notification.permission === "granted") {
      new Notification("流量暴增預警", { body: msg, tag: "traffic-surge" });
    }
  } catch (e) {}
}

// ===== 動態 favicon(跳用量提醒時分頁圖示右上角出現紅點)=====
// 紅點 =「還沒看的提醒」:達門檻跳提醒時亮,點掉紅橫幅(或關閉用量通知)後就熄。
let faviconEl = null;
let faviconState = null;   // 記住目前畫的狀態,避免每次輪詢都重畫

function ensureFavicon() {
  if (faviconEl) return faviconEl;
  faviconEl = document.querySelector("link[rel~='icon']");
  if (!faviconEl) {
    faviconEl = document.createElement("link");
    faviconEl.rel = "icon";
    document.head.appendChild(faviconEl);
  }
  return faviconEl;
}

// canvas 的 fillStyle 不認得 var(--x),必須先把 token 解析成實際色值。
// 讀不到就退回原本的預設色(例如 CSS 還沒載入完就先畫了 favicon)。
function cssColor(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function drawFavicon(alert) {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const x = c.getContext("2d");
  // 底:主色圓角方塊 + 三根長條(流量意象)
  x.fillStyle = cssColor("--btn", "#853bf6");
  if (x.roundRect) { x.beginPath(); x.roundRect(4, 4, 56, 56, 14); x.fill(); }
  else x.fillRect(4, 4, 56, 56);
  x.fillStyle = "rgba(255,255,255,.92)";
  x.fillRect(15, 36, 9, 14); x.fillRect(28, 27, 9, 23); x.fillRect(41, 18, 9, 32);
  // 達門檻:右上角紅點(白邊,任何底色上都明顯)
  if (alert) {
    x.beginPath(); x.arc(50, 14, 13, 0, Math.PI * 2); x.fillStyle = "#fff"; x.fill();
    x.beginPath(); x.arc(50, 14, 9, 0, Math.PI * 2);
    x.fillStyle = cssColor("--danger", "#e5484d"); x.fill();
  }
  return c.toDataURL("image/png");
}

function updateFavicon(alert) {
  const st = alert ? "alert" : "normal";
  if (st === faviconState) return;   // 狀態沒變就不重設(避免閃爍)
  faviconState = st;
  try { ensureFavicon().href = drawFavicon(alert); } catch (e) {}
}

// 換外觀時由 index.html 的 applyTheme 呼叫。favicon 是 canvas 畫的,吃的是解析後的
// 色值而非 var(),所以換色後得重畫一次 —— 清掉狀態快取讓它重跑(狀態本身不變)。
window.repaintFavicon = function () {
  const wasAlert = faviconState === "alert";
  faviconState = null;
  updateFavicon(wasAlert);
};

// 紅點 =「還有未看的提醒」,由用量通知與流量暴增共用。任一橫幅還顯示著就保持亮,
// 兩者都關掉(按掉或關開關)才熄 —— 故所有「按掉/關閉」路徑都要呼叫這支重算,
// 不可逕自 updateFavicon(false)(會把另一種還沒看的提醒紅點也誤熄)。
function refreshAlertDot() {
  const nb = document.getElementById("notifybanner");
  const sb = document.getElementById("surgebanner");
  const anyShown = (nb && nb.style.display === "block")
                || (sb && sb.style.display === "block");
  updateFavicon(anyShown);
}

// ===== 限速偵測(網速) =====
function bpsToMbps(bps) { return (bps || 0) * 8 / 1e6; }

function restoreSpeedFloor() {
  let v;
  try { v = localStorage.getItem("speedFloor"); } catch (e) {}
  if (v != null) {
    speedFloor = Math.max(0, parseFloat(v) || 5);
    speedFloorUserSet = true;
  }
  const el = document.getElementById("spfloor");
  if (el) el.value = speedFloor;
}

function saveSpeedFloor() {
  let v = parseFloat(document.getElementById("spfloor").value);
  if (!Number.isFinite(v) || v < 0) v = 5;
  speedFloor = v;
  speedFloorUserSet = true;
  document.getElementById("spfloor").value = v;
  try { localStorage.setItem("speedFloor", String(v)); } catch (e) {}
  renderPeak();   // 門檻變了 → 重算被動判定
}

// 被動天花板:高峰速率「曾達正常速率」就能確定沒被限速;偏低則無法斷定(可能只是閒置)。
function renderPeak() {
  const pdEl = document.getElementById("peakdown");
  if (!pdEl) return;
  document.getElementById("peakwin").textContent = lastPeakWinMin ? Math.round(lastPeakWinMin) : "—";
  const mbps = bpsToMbps(lastPeakBps);
  pdEl.textContent = mbps >= 0.01 ? mbps.toFixed(2) + " Mbps" : "—";
  const ok = mbps >= speedFloor;
  pdEl.style.color = ok ? "var(--ok-ink)" : "var(--dim)";
  const vEl = document.getElementById("peakverdict");
  if (ok) {
    vEl.textContent = "✓ 未被限速";
    vEl.style.color = "var(--ok-ink)";
  } else {
    // 偏低無法斷定:不佔一整行,改放資訊 icon,滑入看說明
    vEl.innerHTML = '<i class="info" tabindex="0" data-tip="速率偏低,無法斷定 — 可能只是閒置(沒在下載、不耗流量),也可能被限速;按「測速」可確認。"></i>';
    vEl.style.color = "var(--dim)";
  }
}

async function runSpeedTest() {
  const btn = document.getElementById("spbtn");
  const res = document.getElementById("sptresult");
  const verdict = document.getElementById("sptverdict");
  const toast = document.getElementById("sptoast");
  btn.disabled = true;
  toast.textContent = "";
  res.textContent = "測速中…"; res.style.color = "var(--dim)";
  verdict.textContent = "";
  try {
    const d = await (await fetch("/api/speedtest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ floor_mbps: speedFloor }),
    })).json();
    if (d.ok) {
      res.textContent = d.mbps.toFixed(2) + " Mbps";
      if (d.throttled) {
        res.style.color = "var(--danger)";
        verdict.textContent = `⚠ 疑似被限速(< ${d.floor_mbps}M)`;
        verdict.style.color = "var(--danger)";
      } else {
        res.style.color = "var(--ok-ink)";
        verdict.textContent = "✓ 未被限速";
        verdict.style.color = "var(--ok-ink)";
      }
      toast.textContent = `· ${fmtBytes(d.bytes)} / ${d.seconds}s`;
    } else {
      res.textContent = "—"; res.style.color = "var(--dim)";
      verdict.textContent = "測速失敗:" + (d.message || "");
      verdict.style.color = "var(--warn)";
    }
  } catch (e) {
    res.textContent = "—"; res.style.color = "var(--dim)";
    verdict.textContent = "測速失敗:" + e;
    verdict.style.color = "var(--warn)";
  } finally {
    btn.disabled = false;
  }
}

// ===== 連線健康(前端自己用牆鐘判斷,補後端 data_stale 看不到的盲點)=====
// 後端的停更偵測(data_stale → updateHealthBanner)只有在「成功拿到回應」時才看得到。
// 但若後端整個關掉、連線中斷、或電腦睡眠醒來後 socket 失效,fetch 會直接失敗 ——
// 以前 catch 把錯誤吞掉,頁面就這樣靜悄悄凍住(數字與「最後更新」都不動)卻毫無提示。
// 這裡用前端自己的牆鐘記「上次成功更新」,連續抓不到超過門檻就主動跳橫幅,
// 把「靜默凍住」變成看得見的警示。
let lastStatusOkAt = Date.now();   // 上次成功拿到 /api/status 的牆鐘時間(ms)
let connLostDismissed = false;     // 使用者按掉了「無法連線」橫幅;重新連上後再清掉
const CONN_LOST_AFTER_MS = 30000;  // 連續抓不到超過此秒數(約 6 次輪詢)才跳,避免單次閃斷誤報

function showConnLost() {
  if (connLostDismissed) return;
  const hb = document.getElementById("healthbanner");
  if (!hb) return;
  const ageSec = Math.round((Date.now() - lastStatusOkAt) / 1000);
  hb.innerHTML = `⚠️ 已 ${ageSec} 秒無法連線到流量監控 —— 程式可能已關閉,或電腦剛從睡眠喚醒。`
    + `<div style="margin-top:6px; font-weight:400; font-size:var(--fs-hint);">`
    + `請確認執行視窗仍開著;若已關閉,重新執行 start_monitor.bat 即可。(點此關閉)</div>`;
  hb.style.display = "block";
}

// ===== 今日流量錶 + 網卡明細(每 5 秒輪詢)=====
async function tickStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    lastStatus = s;
    // 成功更新:刷新牆鐘、解除「無法連線」抑制(下次再斷線會重新提醒)。
    lastStatusOkAt = Date.now();
    connLostDismissed = false;
    document.getElementById("restartbanner").style.display = s.needs_restart ? "block" : "none";
    updateHealthBanner(s);
    document.getElementById("used").textContent = (s.used_bytes / 1073741824).toFixed(3);
    document.getElementById("quota").textContent = s.quota_gb;
    document.getElementById("date").textContent = s.date;
    document.getElementById("updated").textContent = s.updated_at;
    document.getElementById("remain").textContent = fmtBytes(s.remaining_bytes);
    document.getElementById("pct").textContent = s.percent.toFixed(1) + " %";
    document.getElementById("down").textContent = fmtRate(s.rate_down);
    document.getElementById("up").textContent = fmtRate(s.rate_up);
    const lanEl = document.getElementById("lanexcl");
    if (lanEl) {
      const ex = s.lan_excluded_bytes || 0;
      lanEl.textContent = ex > 0 ? fmtBytes(ex) : "—";
      lanEl.style.color = ex > 0 ? "var(--ok-ink)" : "var(--dim)";
    }

    const color = s.percent >= 90 ? "var(--danger)"
                : s.percent >= 70 ? "var(--warn)" : "var(--btn)";
    // 兩個色標之間留 0.6 度的漸層帶:角度寫成一樣(硬切)時瀏覽器不做抗鋸齒,
    // 進度與底色的交界會看到階梯狀的鋸齒。留一點過渡空間就平滑了,寬度小到看不出漸層。
    const deg = Math.min(100, s.percent) * 3.6;
    document.getElementById("u-donut").style.background =
      `conic-gradient(${color} ${deg}deg, var(--track) ${deg + 0.6}deg)`;
    document.getElementById("u-rem").textContent = "今日已用";
    document.getElementById("u-ratio").textContent =
      (s.used_bytes / 1073741824).toFixed(2) + " / " + s.quota_gb + " GB";

    checkUsageNotify(s.percent, s.date);

    lastPeakBps = s.peak_down_window || 0;
    lastPeakWinMin = s.peak_window_min || 0;
    renderPeak();

    renderSurges(s);
    renderApps(s);
    maybeRefreshCalcLog(s);
    renderSurgeLog(s);

    const tb = document.getElementById("nics");
    tb.innerHTML = "";
    const nics = Object.entries(s.per_nic).sort((a, b) =>
      (b[1].sent + b[1].recv) - (a[1].sent + a[1].recv));
    for (const [name, v] of nics) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${name}</td>`
        + `<td class="num">${fmtBytes(v.recv)}</td>`
        + `<td class="num">${fmtBytes(v.sent)}</td>`
        + `<td class="num">${fmtBytes(v.sent + v.recv)}</td>`;
      tb.appendChild(tr);
    }
  } catch (e) {
    // 拿不到 /api/status:後端關閉、連線中斷、或電腦剛睡眠喚醒使 socket 失效。
    // 以前這裡靜默吞掉,頁面就無聲凍住 —— 改成連續失敗超過門檻就跳「無法連線」橫幅。
    if (Date.now() - lastStatusOkAt > CONN_LOST_AFTER_MS) showConnLost();
  }
}

// ===== 讀設定(/api/config)=====
// 開頁讀一次:速率下限預設值 + 伺服器本機偏好(通知、暴增、內網排除、外觀)。
// 偏好以伺服器為準(prefs_<機器>.json,換瀏覽器/清快取後仍在);各開關在 onInit 先用
// localStorage 預填,這裡覆寫。#quota 不在這裡填 —— tickStatus 每 5 秒會用 s.quota_gb 寫入。
async function loadMonitorConfig() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    // 使用者沒自訂過速率下限才採用 config 的值
    if (!speedFloorUserSet && cfg.speed_floor_mbps != null) {
      speedFloor = cfg.speed_floor_mbps;
      const el = document.getElementById("spfloor");
      if (el) el.value = speedFloor;
      renderPeak();
    }
    applyNotifyFromServer(cfg.notify_enabled, cfg.notify_threshold);
    applySurgeFromServer(cfg.surge_enabled, cfg.surge_min_mb);
    applyExcludeLanFromServer(cfg.exclude_lan);
    // 外觀(applyTheme 在 index.html)。save=false:讀回來的既有值不必再寫回去;
    // 伺服器沒存過(null)時把目前套用中的那個種上去。
    if (cfg.theme) applyTheme(cfg.theme, false);
    else applyTheme(currentTheme());
  } catch (e) {}
}

// ===== 伺服器健康橫幅 =====
// 資料停更或累積錯誤時提醒重啟。healthBaseline = 頁面載入當下的錯誤數,
// 只報「之後新增」的;按掉後除非又有更多新錯誤(或仍處停更)才會再跳。
let healthBaseline = null, healthDismissedAt = 0, healthLastNew = 0;
function updateHealthBanner(s) {
  const hb = document.getElementById("healthbanner");
  const errs = s.server_errors || 0;
  if (healthBaseline === null) healthBaseline = errs;
  const newErrs = errs - healthBaseline;
  healthLastNew = newErrs;
  const stale = !!s.data_stale;
  if (!stale && (newErrs <= 0 || newErrs <= healthDismissedAt)) {
    hb.style.display = "none";
    return;
  }
  const parts = [];
  if (stale && s.data_age_sec != null) parts.push(`資料已 ${Math.round(s.data_age_sec)} 秒沒更新`);
  if (newErrs > 0) parts.push(`發生 ${newErrs} 個錯誤`);
  let html = `⚠️ ${parts.join("、")} —— 程式可能卡住了,建議到執行視窗按 Ctrl+C 重新啟動。`;
  if (s.recent_errors && s.recent_errors.length) {
    html += `<div style="margin-top:6px; font-weight:400; font-size:var(--fs-hint); white-space:pre-wrap;">`
          + s.recent_errors.join("\n").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]))
          + `</div>`;
  }
  hb.innerHTML = html;
  hb.style.display = "block";
}
function dismissHealth() {
  healthDismissedAt = healthLastNew;   // 記住已知到第幾個錯誤;之後超過才再跳
  connLostDismissed = true;            // 也按掉「無法連線」橫幅;重新連上後會自動再啟用提醒
  document.getElementById("healthbanner").style.display = "none";
}

// ===== 頁籤註冊 =====
// onInit 收下的幾行原本在 main.js 開頭 —— 它們全是流量監控頁的事(通知門檻、
// 暴增預警、內網排除、測速下限、面板收合、面板內頁籤),放在這裡
// 才跟實作在同一個檔。updateFavicon 也是本檔的,一併搬過來。
registerTab({
  id: "monitor",
  label: "流量監控",
  order: 10,
  onInit() {
    restoreNotify();          // 用量通知
    restoreSurge();           // 流量暴增預警
    restoreExcludeLan();      // 排除內網流量(稍後由 loadMonitorConfig 以伺服器為準覆寫)
    restoreSpeedFloor();      // 測速下限
    restoreCalcLogCollapse(); // 整合面板(網卡+用量明細)的收合/展開狀態(預設展開)
    restoreMonitorTab();      // 整合面板停在哪個頁籤(預設網卡)
    updateFavicon(false);     // 先畫上正常分頁圖示(關掉提醒後會回到這個)
  },
});
