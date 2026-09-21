// ===== 檢索進度(本月) =====
// 內容:本月累積/還需檢索/剩餘工作日、達標環圈,今日進度小區塊(renderToday),
//       今日達標慶祝(checkDailyGoal / celebrateGoal)。
// 相依 API:/api/monthly、/api/analytics。
// 與「爬取統計」共用 anData / pickVal(定義在 analytics.js):今日已檢索由那邊來。

// ----- 狀態 -----
let monthlyData = null;       // 最近一次本月進度資料(供今日達標慶祝判斷用 per_workday)

// 匯出檔防呆橫幅:後端 export_health() 認出的狀況(缺檔/兩份存反檔名/認不出種類)。
// 種類一律依表頭內容判斷,數字不受影響;這條純粹是叫使用者把檔名改回來,免得一錯再錯。
function renderExportWarn(m) {
  const box = document.getElementById("exportwarn");
  if (!box) return;
  const ws = (m && m.export_warnings) || [];
  if (!ws.length) { box.style.display = "none"; return; }
  box.innerHTML = `⚠ 匯出檔要注意:<br>`
    + ws.map((w) => `・${w}`).join("<br>")
    + `<br><span style="font-weight:400;">數字已依檔案內容(表頭)正確歸類,不影響統計;`
    + `但請把 data/exports/ 的檔名改正,以免日後混淆。</span>`;
  box.style.display = "";
}

function renderMonthly(m) {
  monthlyData = m;
  const id = (x) => document.getElementById(x);
  renderExportWarn(m);
  id("m-month").textContent = m.month || "—";
  id("m-target").textContent = (m.monthly_target || 0).toLocaleString();

  if (!m.ok || !m.files_found) {
    for (const k of ["m-total", "m-crawl", "m-workdays", "m-pct", "m-rem", "m-ratio"])
      id(k).textContent = "—";
    id("m-status").textContent = m.message
      ? ("無法計算:" + m.message)
      : "data/exports/ 找不到含「簡易版」「API」的 .xls;放進去後按「更新計算」。";
    return;
  }

  const pct = m.percent || 0;
  const done = m.on_track || (m.crawl_needed || 0) <= 0;   // 照目前速度月底即可達標
  id("m-total").textContent = (m.total || 0).toLocaleString() + " 筆";
  id("m-crawl").textContent = done ? "0(趨勢達標)" : (m.crawl_needed || 0).toLocaleString() + " 筆";
  id("m-workdays").textContent = (m.workdays_remaining || 0) + " 個";

  const reached = pct >= 100;                              // 達標 → 轉金(獎勵色);未達標 → 紫(進行中)
  const color = reached ? "var(--gold)" : "var(--btn)";
  id("m-donut").style.background =
    `conic-gradient(${color} ${Math.min(100, pct) * 3.6}deg, var(--track) 0deg)`;
  id("m-pct").textContent = pct.toFixed(1) + "%";
  id("m-pct").style.color = reached ? "var(--gold-ink)" : "var(--txt)";
  id("m-rem").textContent = "本月累積";
  id("m-ratio").textContent =
    (m.total || 0).toLocaleString() + " / " + (m.monthly_target || 0).toLocaleString();

  id("m-status").textContent = m.data_mtime
    ? ("資料更新於 " + m.data_mtime)
    : ("資料到 " + fmtMD(m.data_date));
}

// 今日小區塊(顯示在爬取統計頁):今日目標 = per_workday(每個工作日需檢索);
// 今日已檢索 = 本工具今日跑掉的筆數。per_workday 為 0 代表「趨勢達標」或「本月無工作日」
// → 今日無硬性目標,不顯示「還需」。
function renderToday() {
  const id = (x) => document.getElementById(x);
  const m = monthlyData || {};
  // 只有「算得出可靠月度」時才有今日目標;沒匯出檔(files_found=false)時 per_workday 是用
  // 預設目標硬推的、沒實際依據,視為無目標 → 退化成只顯示「今日已完成」,不顯示誤導的目標數字。
  const hasMonthly = !!(m.ok && m.files_found);
  const target = hasMonthly ? (Number(m.per_workday) || 0) : 0;
  const onTrack = hasMonthly && (m.on_track || (m.crawl_needed || 0) <= 0);
  const done = (anData && anData.today && anData.today.queries) || 0;   // 全機合計(本工具今日檢索)
  const head = id("t-headline");
  const bar = id("t-bar");
  // 今日已完成不另外顯示:下方統計的「檢索筆數」即此值,避免同一數字在同一卡片重複出現。

  const wrap = id("t-target-wrap");
  if (target <= 0) {   // 無硬性目標
    // 趨勢達標(今日無需額外檢索)→ 沒有今日目標可言,整個「今日目標」字樣不顯示。
    if (wrap) wrap.style.display = onTrack ? "none" : "";
    id("t-target").textContent = "—";
    head.innerHTML = onTrack
      ? `<span style="color:var(--gold-ink);">✓ 今日無需額外檢索</span>`
      : `今日已完成 <span style="color:var(--btn-ink);">${done.toLocaleString()}</span> 筆`;
    bar.style.width = onTrack ? "100%" : "0%";
    bar.style.background = onTrack ? "var(--gold)" : "var(--btn)";
    return;
  }

  if (wrap) wrap.style.display = "";
  id("t-target").textContent = target.toLocaleString();
  const remain = Math.max(0, target - done);
  const pct = Math.min(100, Math.round(done / target * 100));
  if (remain <= 0) {
    head.innerHTML = `<span style="color:var(--gold-ink);">🎉 今日已達標</span> <span style="font-size:var(--fs-h2);color:var(--dim);font-weight:500;">`
      + `(已完成 ${done.toLocaleString()} / ${target.toLocaleString()})</span>`;
    bar.style.background = "var(--gold)";
  } else {
    head.innerHTML = `今日還需 <span style="color:var(--btn-ink);">${remain.toLocaleString()}</span> `
      + `<span style="font-size:var(--fs-h2);color:var(--dim);font-weight:500;">筆</span>`;
    bar.style.background = "var(--btn)";
  }
  bar.style.width = pct + "%";
}

// ===== 今日達標慶祝 =====
// 達標定義:今日檢索筆數 ≥「每個工作日需檢索」(per_workday)。
// per_workday 為 0(或無)時不做達標計算,不觸發任何效果。一天最多慶祝一次。
function checkDailyGoal() {
  if (!anData || !monthlyData) return;
  if (!(monthlyData.ok && monthlyData.files_found)) return;   // 沒可靠月度(無匯出檔)→ 目標無依據,不判達標
  const target = Number(monthlyData.per_workday) || 0;
  if (target <= 0) return;                          // 門檻為 0 → 不計算達標
  const today = anData.today && anData.today.date;
  if (!today) return;
  const done = pickVal(anData.today, "queries");
  if (done < target) return;
  if (localStorage.getItem("flowcheck_goal_celebrated") === today) return;  // 今天已慶祝過
  localStorage.setItem("flowcheck_goal_celebrated", today);
  celebrateGoal();
}

function celebrateGoal() {
  const fx = document.getElementById("goalfx");
  const toast = document.getElementById("goaltoast");
  // 彩帶用 token(設在 style.background 上瀏覽器會自己解析 var()),換配色時一起跟著變
  const colors = ["var(--gold)", "var(--btn)", "var(--blue)", "var(--ok)",
                  "var(--danger)", "var(--accent)"];
  fx.innerHTML = "";
  for (let i = 0; i < 90; i++) {
    const p = document.createElement("div");
    p.className = "confetti";
    // 無 Math.random 限制(瀏覽器端可用),用它做自然散落
    p.style.left = (Math.random() * 100) + "vw";
    p.style.background = colors[i % colors.length];
    p.style.animationDuration = (2.4 + Math.random() * 1.8) + "s";
    p.style.animationDelay = (Math.random() * 0.5) + "s";
    p.style.transform = "scale(" + (0.7 + Math.random() * 0.8) + ")";
    fx.appendChild(p);
  }
  fx.classList.add("on");
  toast.classList.add("on");
  setTimeout(() => toast.classList.remove("on"), 3600);
  setTimeout(() => { fx.classList.remove("on"); fx.innerHTML = ""; }, 5000);
}

function fmtGoalNum(n) { return Math.round(Number(n) || 0).toLocaleString(); }
function fmtGoalPct(n) { return (Number(n) || 0).toFixed(1) + "%"; }

let gaData = null;
let gaMode = "total";
let gaMonth = "";

const GOAL_MODE = {
  total: { label: "總數", field: "total", line: "actual-line", point: "total" },
  methods: { label: "簡易版＋API", field: "", line: "", point: "" },
};

const gaMethodVisible = { simple: true, api: true };

function setGoalAnalysisStatus(text) {
  document.getElementById("ga-status").textContent = text || "";
}

function setGoalMode(mode) {
  if (!GOAL_MODE[mode]) return;
  gaMode = mode;
  for (const name of Object.keys(GOAL_MODE)) {
    const btn = document.getElementById("ga-mode-" + name);
    const on = name === gaMode;
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-selected", String(on));
  }
  document.getElementById("ga-display-toggle").style.display = gaMode === "methods" ? "flex" : "none";
  renderGoalAnalysis(gaData);
}

function toggleMethodLine(method, visible) {
  gaMethodVisible[method] = visible;
  if (gaMode === "methods") renderGoalAnalysis(gaData);
}

function changeGoalMonth(month) {
  if (month && month !== gaMonth) loadGoalAnalysis(month);
}

function buildGoalMonths(t) {
  const select = document.getElementById("ga-month");
  const months = Array.isArray(t && t.months) ? t.months : [];
  const selected = t && t.month || gaMonth;
  select.innerHTML = months.length
    ? months.map(m => `<option value="${m}">${m}</option>`).join("")
    : `<option value="">尚無紀錄</option>`;
  select.value = selected && months.includes(selected) ? selected : (months[0] || "");
  select.disabled = !months.length;
}

function goalModePoints(t) {
  const points = Array.isArray(t && t.points) ? t.points : [];
  if (gaMode === "total") return points;
  // 合併圖只保留至少有一種方式資料的更新點；每次更新仍是同一個 X 軸位置。
  return points.filter(p => p.simple_available || p.api_available);
}

function renderGoalLegend() {
  const legend = document.getElementById("ga-legend");
  if (gaMode === "total") {
    legend.innerHTML = `<span><i></i>總累積筆數</span><span><i class="target"></i>月目標</span>`;
    return;
  }
  let html = "";
  if (gaMethodVisible.simple) html += `<span><i class="simple"></i>簡易版累積筆數</span>`;
  if (gaMethodVisible.api) html += `<span><i class="api"></i>API 累積筆數</span>`;
  legend.innerHTML = html || `<span>請至少選擇一種顯示方式</span>`;
}

function renderGoalAnalysis(t) {
  gaData = t || { month: "", months: [], points: [] };
  gaMonth = gaData.month || gaMonth;
  buildGoalMonths(gaData);
  renderGoalLegend();
  const allPoints = Array.isArray(gaData.points) ? gaData.points : [];
  const points = goalModePoints(gaData);
  if (!allPoints.length) {
    renderGoalChart([], GOAL_MODE[gaMode]);
    setGoalAnalysisStatus("尚無趨勢資料。");
    return;
  }
  if (!points.length) {
    renderGoalChart([], GOAL_MODE[gaMode]);
    setGoalAnalysisStatus(gaMode === "methods" ? "簡易版與 API 尚無更新紀錄。" : "總數尚無更新紀錄。");
    return;
  }
  renderGoalChart(points, GOAL_MODE[gaMode]);
  setGoalAnalysisStatus("");
}

function renderGoalChart(points, mode) {
  const svg = document.getElementById("ga-chart");
  const tip = document.getElementById("ga-tooltip");
  const isTotal = gaMode === "total";
  const title = isTotal ? "總數檢索趨勢圖" : "簡易版與 API 檢索趨勢圖";
  const desc = isTotal
    ? "左軸為總累積檢索筆數，金色虛線為本月目標。"
    : "左軸為每次更新時記錄的簡易版與 API 累積檢索筆數。";
  if (!points.length) {
    svg.innerHTML = `<title id="ga-chart-title">${title}</title><desc id="ga-chart-desc">${desc}</desc>`
      + `<text x="380" y="150" text-anchor="middle" class="axis-label">尚無可顯示資料</text>`;
    tip.style.display = "none";
    return;
  }

  const W = 760, H = 300, L = 68, R = 20, T = 22, B = 42;
  const iw = W - L - R, ih = H - T - B;
  const visibleFields = isTotal ? ["total"] : [
    ...(gaMethodVisible.simple ? ["simple_total"] : []),
    ...(gaMethodVisible.api ? ["api_total"] : []),
  ];
  const values = visibleFields.flatMap(field => points.map(p => Number(p[field] || 0)));
  const targets = isTotal ? points.map(p => Number(p.monthly_target || 0)) : [];
  const maxValue = Math.max(1, ...values, ...targets);
  const unit = Math.pow(10, Math.max(0, Math.floor(Math.log10(maxValue))));
  const countMax = Math.ceil(maxValue * 1.1 / unit) * unit;
  const pctOf = p => Number(p.monthly_target || 0)
    ? Number(p.total || 0) / Number(p.monthly_target) * 100 : 0;
  const x = i => points.length === 1 ? L + iw / 2 : L + iw * i / (points.length - 1);
  const yCount = value => T + ih * (1 - Number(value || 0) / countMax);
  const path = field => points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${yCount(p[field]).toFixed(1)}`).join(" ");
  const xStep = Math.max(1, Math.ceil(points.length / 8));
  let html = `<title id="ga-chart-title">${title}</title><desc id="ga-chart-desc">${desc}</desc>`;
  for (let i = 0; i <= 4; i++) {
    const count = countMax * i / 4, yy = yCount(count);
    html += `<line class="grid" x1="${L}" y1="${yy}" x2="${W - R}" y2="${yy}"></line>`;
    html += `<text class="axis-label" x="${L - 8}" y="${yy + 4}" text-anchor="end">${fmtGoalNum(count)}</text>`;
  }
  if (isTotal) {
    html += `<path class="target-line" d="${path("monthly_target")}"></path>`;
    html += `<path class="actual-line" d="${path("total")}"></path>`;
  } else {
    if (gaMethodVisible.simple) html += `<path class="simple-line" d="${path("simple_total")}"></path>`;
    if (gaMethodVisible.api) html += `<path class="api-line" d="${path("api_total")}"></path>`;
  }
  points.forEach((p, i) => {
    if (i % xStep === 0 || i === points.length - 1) {
      // X 軸使用輸入匯出檔的資料日期，而非使用者按更新按鈕的時間。
      const label = String(p.date || "").slice(5).replace("-", "/");
      html += `<text class="axis-label" x="${x(i)}" y="${H - 16}" text-anchor="middle">${label}</text>`;
    }
    if (isTotal) {
      html += `<circle class="point total" cx="${x(i)}" cy="${yCount(p.total)}" r="5" tabindex="0" data-point="${i}" data-method="total" aria-label="總數 ${fmtGoalNum(p.total)} 筆，${fmtGoalPct(pctOf(p))}"></circle>`;
    } else {
      if (gaMethodVisible.simple && p.simple_available) {
        html += `<circle class="point simple" cx="${x(i)}" cy="${yCount(p.simple_total)}" r="5" tabindex="0" data-point="${i}" data-method="simple" aria-label="簡易版 ${fmtGoalNum(p.simple_total)} 筆"></circle>`;
      }
      if (gaMethodVisible.api && p.api_available) {
        html += `<circle class="point api" cx="${x(i)}" cy="${yCount(p.api_total)}" r="5" tabindex="0" data-point="${i}" data-method="api" aria-label="API ${fmtGoalNum(p.api_total)} 筆"></circle>`;
      }
    }
  });
  svg.innerHTML = html;
  const showTip = (event, point, method) => {
    const detail = method === "total"
      ? `總數 ${fmtGoalNum(point.total)} 筆<br>${fmtGoalPct(pctOf(point))}`
      : method === "simple"
        ? `簡易版 ${fmtGoalNum(point.simple_total)} 筆`
        : `API ${fmtGoalNum(point.api_total)} 筆`;
    tip.innerHTML = detail;
    tip.style.display = "block";
    const box = svg.getBoundingClientRect();
    const px = event.clientX ? event.clientX - box.left : box.width / 2;
    tip.style.left = `${Math.min(box.width - 220, Math.max(8, px + 8))}px`;
    tip.style.top = "12px";
  };
  svg.querySelectorAll(".point").forEach(el => {
    const point = points[Number(el.dataset.point)];
    const method = el.dataset.method;
    el.addEventListener("mouseenter", e => showTip(e, point, method));
    el.addEventListener("focus", e => showTip(e, point, method));
    el.addEventListener("mouseleave", () => { tip.style.display = "none"; });
    el.addEventListener("blur", () => { tip.style.display = "none"; });
  });
}

async function loadGoalAnalysis(month = "") {
  const spin = document.getElementById("ga-spin");
  spin.style.display = "inline-block";
  try {
    const query = month ? "?month=" + encodeURIComponent(month) : "";
    renderGoalAnalysis(await fetch("/api/monthly/trend" + query).then((r) => r.json()));
  } catch (e) {
    renderGoalAnalysis(null);
    setGoalAnalysisStatus("趨勢資料讀取失敗，請稍後再試。");
  } finally {
    spin.style.display = "none";
  }
}

async function refreshMonthly(recordSnapshot = false) {
  const spin = document.getElementById("m-spin");
  spin.style.display = "inline-block";
  document.getElementById("m-status").textContent = "計算中…";
  try {
    // 只有按鈕觸發的更新才 POST 並留存快照；首次開啟頁面僅讀取當前數據，不建立假資料點。
    const monthlyUrl = recordSnapshot ? "/api/monthly/refresh" : "/api/monthly";
    const [m, a] = await Promise.all([
      fetch(monthlyUrl, recordSnapshot ? { method: "POST" } : undefined).then((r) => r.json()),
      fetch("/api/analytics").then((r) => r.json()).catch(() => null),
    ]);
    if (a) anData = a;
    renderMonthly(m);
    if (m.trend) renderGoalAnalysis(m.trend); else await loadGoalAnalysis();
  }
  catch (e) { document.getElementById("m-status").textContent = "讀取失敗:" + e; }
  finally { spin.style.display = "none"; }
}

// ===== 頁籤註冊 =====
// 只在第一次開啟時抓現有資料,之後切分頁不再自動重算(資料來自手動下載的匯出檔,
// 沒換檔數字也不會變);要更新按「更新計算」。舊版是用一個全域的 calcLoaded 旗標
// 自己記,現在由 onFirstShow 的語意直接表達,那個旗標已移除。
registerTab({
  id: "calc",
  label: "檢索進度",
  order: 40,
  onFirstShow() { refreshMonthly(); },
});
