// ===== 流量報表 =====
// 同一個圖表區塊兩種畫面:每天一根堆疊柱(預設);點某一天,區塊直接換成那天 8–20 點的
// 逐時折線圖 —— 三支程式各一條線、同一個刻度,看得出 ssh / Claude Code / VS Code 是不是
// 同時段一起升降(堆疊柱只看得出總量,看不出彼此的關聯)。「← 每日」回來。
// 用途是截圖跟公司反應連線回公司(ssh)的流量,所以 ssh 貼著底線放最下面,最好比。
// 相依 API:/api/traffic/daily。

// 堆疊順序(由下往上)。顏色在 style.css / style-classic.css 的 .rp-s-* / .rp-k-* 定義。
// 「其他」= 總量扣掉這三項(含對不到程式的量,不另外列)。
const RP_SERIES = [
  { key: "ssh",    label: "ssh" },
  { key: "claude", label: "Claude Code" },
  { key: "vscode", label: "VS Code" },
  { key: "other",  label: "其他" },
];
const RP_WEEK = "日一二三四五六";
const RP_DIR_LABEL = { sum: "合計", up: "上傳", down: "下載" };

// 逐時圖只畫上班時段:8 點到 20 點(含 20 點那一格,即 20:00–21:00)。
const RP_HOUR_FROM = 8, RP_HOUR_TO = 20;

let rpData = null;
let rpDir = "sum";      // sum | up | down
let rpRange = 14;       // 最近幾個「有紀錄的日子」;0 = 全部
let rpDay = null;       // 正在看逐時的那一天;null = 看每日

// v = [收, 發] → 依目前方向取一個數。
function rpPick(v) {
  v = v || [0, 0];
  const r = v[0] || 0, s = v[1] || 0;
  return rpDir === "up" ? s : rpDir === "down" ? r : r + s;
}

// 一筆(一天,或一天裡的一小時)的各段量。apps 是 {ssh: [收, 發], ...}。
function rpParts(total, apps) {
  apps = apps || {};
  const p = { ssh: rpPick(apps.ssh), claude: rpPick(apps.claude), vscode: rpPick(apps.vscode) };
  p.total = rpPick(total);
  p.other = Math.max(0, p.total - p.ssh - p.claude - p.vscode);
  return p;
}
const rpDayParts = rec => rpParts(rec.total, rec.apps);
const rpHourParts = h => rpParts(h.total, h);

// 柱頂與統計格用的精簡格式:1.5 GB、320 MB。
function rpFmt(n) {
  const G = 1024 ** 3, M = 1024 ** 2;
  if (n >= G) return (+(n / G).toFixed(n >= 10 * G ? 1 : 2)) + " GB";
  if (n >= M) return Math.round(n / M) + " MB";
  if (n > 0) return Math.max(1, Math.round(n / 1024)) + " KB";
  return "0";
}

// 軸刻度:同一條軸只用一種單位(GB / MB / KB),間距取 1/2/2.5/5×10^k,約 4 格。
function rpAxis(max) {
  const G = 1024 ** 3, M = 1024 ** 2, K = 1024;
  const [unit, name] = max >= G ? [G, "GB"] : max >= M ? [M, "MB"] : [K, "KB"];
  const raw = Math.max(max / unit / 4, unit === G ? 0.1 : 1);
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].find(m => m * pow >= raw) * pow;
  const top = Math.ceil(max / unit / step) * step;
  const ticks = [];
  for (let i = 0; i * step <= top + 1e-9; i++) {
    const v = +(i * step).toFixed(2);
    ticks.push({ value: v * unit, label: v ? v + " " + name : "0" });
  }
  return { top: top * unit, ticks };
}

function rpDateLabel(d) {
  return d.slice(5).replace("-", "/") + "(" + RP_WEEK[new Date(d + "T00:00:00").getDay()] + ")";
}

function rpTag(rec) {
  return rec.source === "live" ? "今日進行中" : "";
}

// 要畫的日子:只有有紀錄的日子(從第一筆紀錄開始,沒紀錄的日子不佔位置)。
function rpDays() {
  const all = Object.keys(rpData.days || {}).sort();
  return rpRange ? all.slice(-rpRange) : all;
}

// 頂端 4px 圓角、底部方角的柱段(只用在一根柱子最上面那段)。
function rpTopRounded(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// 畫一張堆疊柱狀圖。
//   bars: [{ parts | null, label, sub, tag }]   parts 為 null = 該格沒資料(只畫軸標籤)
//   opts: { svg, tip, title, empty, showTop, onPick, tipHead(i) }
function rpDrawBars(bars, opts) {
  const { svg, tip } = opts;
  tip.style.display = "none";
  const W = 760, H = 300, L = 64, R = 14, T = 24, B = 42;
  const iw = W - L - R, ih = H - T - B;
  let html = `<title>${opts.title}</title>`;
  const maxV = Math.max(0, ...bars.map(b => b.parts ? b.parts.ssh + b.parts.claude + b.parts.vscode + b.parts.other : 0));
  if (!maxV) {
    svg.innerHTML = html + `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" class="axis-label">${opts.empty}</text>`;
    return;
  }
  const axis = rpAxis(maxV * 1.1);   // 留一點頭頂空間給柱頂數字
  const y = v => T + ih * (1 - v / axis.top);
  const slot = iw / bars.length;
  const bw = Math.max(3, Math.min(24, slot * 0.6));
  const labelEvery = Math.max(1, Math.ceil(bars.length / 12));

  for (const t of axis.ticks) {
    if (t.value) html += `<line class="grid" x1="${L}" y1="${y(t.value)}" x2="${W - R}" y2="${y(t.value)}"></line>`;
    html += `<text class="axis-label" x="${L - 8}" y="${y(t.value) + 4}" text-anchor="end">${t.label}</text>`;
  }
  bars.forEach((b, i) => {
    const cx = L + slot * (i + 0.5);
    const x0 = cx - bw / 2;
    // 從最新一天往回數,最新那天一定有標籤。
    if ((bars.length - 1 - i) % labelEvery === 0) {
      html += `<text class="axis-label" x="${cx}" y="${H - 22}" text-anchor="middle">${b.label}</text>`;
      if (b.sub) html += `<text class="axis-label" x="${cx}" y="${H - 7}" text-anchor="middle">${b.sub}</text>`;
    }
    const p = b.parts;
    if (p) {
      // 由下往上疊,段與段之間留 2px 空隙(露出底色)。
      const segs = RP_SERIES.filter(s => p[s.key] > 0);
      let acc = 0;
      segs.forEach((s, j) => {
        const yTop = y(acc + p[s.key]);
        const h = y(acc) - yTop - (j ? 2 : 0);
        acc += p[s.key];
        if (h < 0.5) return;
        html += j === segs.length - 1
          ? `<path class="rp-s-${s.key}" d="${rpTopRounded(x0, yTop, bw, h, 4)}"></path>`
          : `<rect class="rp-s-${s.key}" x="${x0}" y="${yTop}" width="${bw}" height="${h}"></rect>`;
      });
      if (opts.showTop && acc > 0) {
        html += `<text class="rp-val" x="${cx}" y="${y(acc) - 6}" text-anchor="middle">${rpFmt(p.total)}</text>`;
      }
    }
    // 整條欄位都是滑鼠/鍵盤的熱區,不必對準細柱子。
    html += `<rect class="rp-hit${opts.onPick ? " pick" : ""}" x="${L + slot * i}" y="${T}" width="${slot}" height="${ih}" tabindex="0" data-i="${i}"></rect>`;
  });
  html += `<line class="rp-base" x1="${L}" y1="${y(0)}" x2="${W - R}" y2="${y(0)}"></line>`;
  svg.innerHTML = html;

  svg.querySelectorAll(".rp-hit").forEach(el => {
    const i = Number(el.dataset.i);
    const hide = () => { el.classList.remove("on"); tip.style.display = "none"; };
    const show = () => rpShowTip(svg, tip, el, opts.tipHead(i), bars[i].parts);
    el.addEventListener("mouseenter", show);
    el.addEventListener("focus", show);
    el.addEventListener("mouseleave", hide);
    el.addEventListener("blur", hide);
    if (opts.onPick) {
      el.addEventListener("click", () => opts.onPick(i));
      el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); opts.onPick(i); } });
    }
  });
}

function rpShowTip(svg, tip, el, headText, p, series = RP_SERIES) {
  svg.querySelectorAll(".rp-hit.on").forEach(x => x.classList.remove("on"));
  el.classList.add("on");
  tip.textContent = "";
  const head = document.createElement("div");
  head.className = "rp-tip-hd";
  head.textContent = headText;
  tip.appendChild(head);
  if (!p || !p.total) {
    tip.appendChild(document.createTextNode("這段時間沒有流量紀錄"));
  } else {
    const rows = [...series].reverse().map(s => [s, p[s.key]]);
    rows.push([{ key: "total", label: "總量" }, p.total]);
    for (const [s, v] of rows) {
      const r = document.createElement("div");
      r.className = "rp-tip-row" + (s.key === "total" ? " total" : "");
      const k = document.createElement("i");
      k.className = "rp-key rp-k-" + s.key;
      const b = document.createElement("b");
      b.textContent = fmtBytes(v);
      const n = document.createElement("span");
      n.textContent = s.label + (s.key !== "total" ? ` ${Math.round(v / p.total * 100)}%` : "");
      r.append(k, b, n);
      tip.appendChild(r);
    }
  }
  tip.style.display = "block";
  // 以外框為座標系(窄螢幕時外框可橫向捲動,要加上捲動量)。
  const wrap = tip.offsetParent || svg.parentElement;
  const box = wrap.getBoundingClientRect();
  const hit = el.getBoundingClientRect();
  const mid = hit.left - box.left + wrap.scrollLeft + hit.width / 2;
  let left = mid + 14;
  if (left + tip.offsetWidth > wrap.scrollLeft + box.width - 4) left = mid - tip.offsetWidth - 14;
  tip.style.left = Math.max(4, left) + "px";
  tip.style.top = "14px";
}

function renderReportDays(days) {
  rpDrawBars(days.map(d => {
    const rec = rpData.days[d];
    return { parts: rpDayParts(rec), label: d.slice(5).replace("-", "/"),
             sub: RP_WEEK[new Date(d + "T00:00:00").getDay()] };
  }), {
    svg: document.getElementById("rp-chart"),
    tip: document.getElementById("rp-tooltip"),
    title: `每日流量(${RP_DIR_LABEL[rpDir]}),由下而上:ssh、Claude Code、VS Code、其他`,
    empty: "還沒有每日統計 —— 監控開著時每天會自動記一筆",
    showTop: days.length <= 16,
    onPick: i => { rpDay = days[i]; renderReport(); },
    tipHead: i => {
      const tag = rpTag(rpData.days[days[i]]);
      return rpDateLabel(days[i]) + " " + RP_DIR_LABEL[rpDir] + (tag ? "・" + tag : "") + "(點一下看逐時)";
    },
  });
}

// 逐時折線只畫三支程式;「其他」對看關聯沒幫助,只在 tooltip 裡列出總量。
const RP_LINES = RP_SERIES.filter(x => x.key !== "other");

function renderReportHours() {
  const d = rpDay;
  const svg = document.getElementById("rp-chart");
  const tip = document.getElementById("rp-tooltip");
  tip.style.display = "none";
  const hours = rpData.days[d].hours || {};
  const W = 760, H = 300, L = 64, R = 24, T = 24, B = 42;
  const iw = W - L - R, ih = H - T - B;
  const hs = [];
  for (let h = RP_HOUR_FROM; h <= RP_HOUR_TO; h++) hs.push(h);
  // 每個鐘點的各段量;沒資料的鐘點 = null(監控沒開或那小時還沒到),線在那裡斷開。
  const pts = hs.map(h => {
    const b = hours[String(h).padStart(2, "0")];
    return b ? rpHourParts(b) : null;
  });
  let html = `<title>${d} 逐時流量(${RP_DIR_LABEL[rpDir]}):ssh、Claude Code、VS Code 各一條線</title>`;
  const maxV = Math.max(0, ...pts.map(p => p ? Math.max(p.ssh, p.claude, p.vscode) : 0));
  if (!maxV) {
    const msg = Object.keys(hours).length ? `${RP_HOUR_FROM}–${RP_HOUR_TO} 點這三支程式沒有流量` : "這天沒有逐時資料";
    svg.innerHTML = html + `<text x="${W / 2}" y="${H / 2}" text-anchor="middle" class="axis-label">${msg}</text>`;
    return;
  }
  const axis = rpAxis(maxV * 1.1);
  const y = v => T + ih * (1 - v / axis.top);
  const x = i => L + iw * i / (hs.length - 1);
  for (const t of axis.ticks) {
    if (t.value) html += `<line class="grid" x1="${L}" y1="${y(t.value)}" x2="${L + iw}" y2="${y(t.value)}"></line>`;
    html += `<text class="axis-label" x="${L - 8}" y="${y(t.value) + 4}" text-anchor="end">${t.label}</text>`;
  }
  hs.forEach((h, i) => {
    html += `<text class="axis-label" x="${x(i)}" y="${H - 16}" text-anchor="middle">${h}點</text>`;
  });
  html += `<line class="rp-base" x1="${L}" y1="${y(0)}" x2="${L + iw}" y2="${y(0)}"></line>`;
  html += `<line class="rp-cross" id="rp-cross" x1="0" y1="${T}" x2="0" y2="${T + ih}" style="display:none;"></line>`;

  // 不在線尾標程式名:三條線常在低處擠在一起,標籤會疊在一起;靠下方圖例 + tooltip 辨識。
  for (const sr of RP_LINES) {
    let path = "", pen = false;
    pts.forEach((p, i) => {
      if (!p) { pen = false; return; }
      path += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(p[sr.key]).toFixed(1)}`;
      pen = true;
    });
    html += `<path class="rp-line rp-l-${sr.key}" d="${path}"></path>`;
    pts.forEach((p, i) => {
      if (p) html += `<circle class="rp-dot rp-d-${sr.key}" cx="${x(i)}" cy="${y(p[sr.key])}" r="3.5" data-i="${i}"></circle>`;
    });
  }
  // 每個鐘點一條垂直熱區:滑到哪個鐘點就顯示那一刻三支程式的量,不用對準細線。
  hs.forEach((h, i) => {
    const x0 = i === 0 ? L : (x(i - 1) + x(i)) / 2;
    const x1 = i === hs.length - 1 ? L + iw : (x(i) + x(i + 1)) / 2;
    html += `<rect class="rp-hit" x="${x0}" y="${T}" width="${x1 - x0}" height="${ih}" tabindex="0" data-i="${i}"></rect>`;
  });
  svg.innerHTML = html;

  const cross = svg.querySelector("#rp-cross");
  svg.querySelectorAll(".rp-hit").forEach(el => {
    const i = Number(el.dataset.i);
    const show = () => {
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
      cross.style.display = "";
      svg.querySelectorAll(".rp-dot.on").forEach(c => c.classList.remove("on"));
      svg.querySelectorAll(`.rp-dot[data-i="${i}"]`).forEach(c => c.classList.add("on"));
      rpShowTip(svg, tip, el, `${d.slice(5).replace("-", "/")} ${hs[i]}:00–${hs[i] + 1}:00 ${RP_DIR_LABEL[rpDir]}`, pts[i], RP_LINES);
      el.classList.remove("on");   // 折線圖用十字線標示位置,不用整欄底色
    };
    const hide = () => {
      cross.style.display = "none";
      svg.querySelectorAll(".rp-dot.on").forEach(c => c.classList.remove("on"));
      tip.style.display = "none";
    };
    el.addEventListener("mouseenter", show);
    el.addEventListener("focus", show);
    el.addEventListener("mouseleave", hide);
    el.addEventListener("blur", hide);
  });
}

// 收合的數字表(圖表的文字版,也是 tooltip 以外查數字的地方)。
function renderReportTable() {
  const cell = v => {
    v = v || [0, 0];
    const s = (v[0] || 0) + (v[1] || 0);
    if (!s) return `<td class="num dim">—</td>`;
    return `<td class="num">${fmtBytes(s)}<div class="hint">↑${fmtBytes(v[1])} ↓${fmtBytes(v[0])}</div></td>`;
  };
  let rows = "";
  for (const [d, rec] of Object.entries(rpData.days || {}).reverse()) {
    const tag = rpTag(rec);
    const a = rec.apps || {};
    rows += `<tr><td>${rpDateLabel(d)}<span class="hint">${tag ? " " + tag : ""}</span></td>`
      + cell(a.ssh) + cell(a.vscode) + cell(a.claude) + cell(rec.total) + "</tr>";
  }
  document.getElementById("rp-rows").innerHTML = rows;
}

function renderReport() {
  if (!rpData) return;
  if (rpDay && !rpData.days[rpDay]) rpDay = null;
  const hourly = !!rpDay;
  document.getElementById("rp-machine").textContent = rpData.machine ? "本機 " + rpData.machine : "";
  document.querySelectorAll("[data-rp-dir]").forEach(b => b.classList.toggle("on", b.dataset.rpDir === rpDir));
  // 逐時畫面:標題換成那一天、出現「← 每日」,期間選單用不到先藏起來。
  const tag = hourly ? rpTag(rpData.days[rpDay]) : "";
  document.getElementById("rp-title").textContent = hourly
    ? rpDateLabel(rpDay) + " 逐時流量" + (tag ? "(" + tag + ")" : "") : "每日流量";
  document.getElementById("rp-back").style.display = hourly ? "" : "none";
  document.getElementById("rp-range-wrap").style.visibility = hourly ? "hidden" : "";
  if (hourly) renderReportHours(); else renderReportDays(rpDays());
  // 圖例跟著圖形走:每日是色塊(柱),逐時是短線(折線),且逐時沒有「其他」。
  document.getElementById("rp-legend").innerHTML = (hourly ? RP_LINES : RP_SERIES)
    .map(s => `<span><i class="rp-key rp-k-${s.key}${hourly ? " line" : ""}"></i>${s.label}</span>`).join("");
  renderReportTable();
}

function setReportDir(dir) { rpDir = dir; renderReport(); }
function setReportRange(n) { rpRange = n; renderReport(); }
function showReportDays() { rpDay = null; renderReport(); }

async function tickReport() {
  try {
    rpData = await (await fetch("/api/traffic/daily")).json();
  } catch (e) {
    return;
  }
  renderReport();
}

registerTab({
  id: "report",
  label: "流量報表",
  order: 15,
  onShow() { tickReport(); },
  // 開著這頁時每分鐘更新一次(今天那根柱子、這個小時那根會長)。
  onInit() { setInterval(() => { if (isTabOpen("report")) tickReport(); }, 60000); },
});
