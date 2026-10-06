// ===== 690資轉 + 信件與連線 =====
// 兩個頁籤共用 /api/xfer690 的快照(伺服器每 10 分鐘抓一次信;這裡每 60 秒讀一次快照)。
// 畫面依 doc/plans/2026-10-02-690資轉畫面草圖.html。信件內容一律用 textContent 放進畫面。
// 相依 API:GET /api/xfer690、POST /api/xfer690/refresh、GET /api/xfer690/log、
//          POST /api/xfer690/wiki/login({jwt})、POST /api/xfer690/wiki/logout、POST /api/xfer690/wiki/write。

const X6_LABEL = { ok: "成功", nodata: "無資料", fail: "失敗", missing: "該到沒到", pending: "還沒跑到",
                   unknown: "無法判定", none: "沒有執行" };
const X6_RANK = ["fail", "missing", "unknown", "pending", "none", "nodata", "ok"];
const X6_BAD = ["fail", "missing", "unknown"];
const X6_TYPES = [["full", "全文"], ["biblio", "書目"], ["sud", "雜項"], ["eng", "英譯"]];
const X6_TRACK = { twa: 2, twb: 3 };       // 只追蹤最近一個月:TWA 每月 2 期、TWB 3 期
const X6_VIEWS = { twa: "TWA 公開", twb: "TWB 公告" };

let x6Snap = null;
let x6View = "daily";
const x6Sel = { twa: null, twb: null };
let x6Edits = {};          // 還沒寫入的手動修改,key = 頁|表|列鍵|欄號
let x6Busy = { refresh: false, login: false, write: false };
let x6Result = null;       // 上次寫入的結果訊息

const x6$ = (id) => document.getElementById(id);
function x6el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const x6worst = (arr) => X6_RANK.find(r => arr.includes(r)) || "none";
// "2026-10-01T02:10:02+08:00" → "10-01 02:10"
const x6mdhm = (iso) => iso ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : "";
const x6hm = (iso) => iso ? iso.slice(11, 16) : "";
const x6md = (isoDate) => isoDate ? isoDate.slice(5).replace("-", "/") : "";
const x6num = (n) => Number(n).toLocaleString();

// ── 期數換算(跟 program/xfer690/periods.py 同一套公式)──
function x6vol(kind, d) {
  const [base, per, span] = kind === "twb" ? [1973, 3, 10] : [2002, 2, 15];
  return (d.getFullYear() - base) * 100 + d.getMonth() * per + Math.floor((d.getDate() - 1) / span) + 1;
}
function x6volDate(kind, vol) {
  const [base, per, span] = kind === "twb" ? [1973, 3, 10] : [2002, 2, 15];
  const n = vol % 100 - 1;
  const m = Math.floor(n / per) + 1, d = (n % per) * span + 1;
  return `${Math.floor(vol / 100) + base}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
// 最近一個月要追蹤的期(今天以前、最新的幾期),新的在前
function x6trackVols(v, today) {
  const days = v === "twb" ? [1, 11, 21] : [1, 16];
  const d = new Date(today + "T00:00:00");
  const out = [];
  for (let i = 0; i < 70 && out.length < X6_TRACK[v]; i++) {
    if (days.includes(d.getDate())) out.push(x6vol(v, d));
    d.setDate(d.getDate() - 1);
  }
  return out;
}

// ── 步驟名稱:主旨的 <名稱> 取 stepN- 後面那段,例如 posted-status-step6-l2-pending-twb → L2P TWB ──
const X6_WORD = { raw: "RAW", os: "OS", l1: "L1", l2: "L2", l3: "L3", twa: "TWA", twb: "TWB",
                  category: "分類", industry: "Industry", status: "狀態", manual: "手動" };
function x6stepLabel(sys, code) {
  const name = ((sys.names || {})[code] || "").replace(/-check$/, "");
  if (!name) return code;
  const side = (name.match(/^(twa|twb)-search/) || [])[1];      // 搜尋的 TWA / TWB 寫在名稱開頭
  const m = name.match(/step\d+-(.+)$/) || name.match(/-(raw-os|raw-manual)$/);
  if (!m) return /create-daily-tables$/.test(name) ? "建表" : code;
  const words = m[1].replace("l2-pending", "l2p").split("-").map(w => w === "l2p" ? "L2P" : (X6_WORD[w] || w));
  return (side ? side.toUpperCase() + " " : "") + words.join(" ");
}
// 一個步驟 + 它的 check 併成一顆點:兩個都成功才算綠
function x6pairState(p, pair) {
  const st = (c) => ((p.steps || {})[c] || {}).state || "none";
  return pair[1] ? x6worst([st(pair[0]), st(pair[1])]) : st(pair[0]);
}
// 失敗的步驟、錯誤訊息、因此沒執行的步驟、可以看 log 的那封信
function x6failInfo(sys, p) {
  const pairs = sys.pairs || [];
  const fp = pairs.find(pr => x6pairState(p, pr) === "fail");
  if (!fp) {
    const af = Object.values(p.auto || {}).find(a => a.state === "fail");
    return af ? { label: "母 job", ev: af.event, err: (af.event || {}).error || "", after: [] } : null;
  }
  const code = pr => ((p.steps[pr[0]] || {}).state === "fail" ? pr[0] : pr[1]);
  const ev = (p.steps[code(fp)] || {}).event;
  const after = pairs.filter(pr => x6pairState(p, pr) === "none").map(pr => x6stepLabel(sys, pr[0]));
  return { label: x6stepLabel(sys, fp[0]), ev, err: (ev && ev.error) || "", after };
}
function x6nodataEvent(p) {
  return Object.values(p.steps || {}).map(s => s.event).find(e => e && e.status === "nodata");
}
function x6writes(sys, p) {
  for (const pr of sys.pairs || []) {
    const e = ((p.steps || {})[pr[0]] || {}).event;
    if (e && e.writes != null) return e.writes;
  }
  return null;
}
function x6runs(p) {
  return Math.max(1, ...Object.values(p.steps || {}).map(s => s.runs || 0));
}

// ── 小元件 ──
function x6dot(state, tip, selKey) {
  const b = x6el("button", "x6-dotbtn");
  b.type = "button";
  b.setAttribute("aria-label", tip.replace(/\n/g, ","));
  b.dataset.tip = tip;
  if (selKey) { b.dataset.sel = selKey; b.setAttribute("aria-pressed", String(x6isSel(selKey))); }
  b.append(x6el("i", "x6-dot " + state));
  return b;
}
function x6stats(box, rows) {
  box.replaceChildren();
  for (const [k, v, c, small] of rows) {
    const s = x6el("div", "x6-stat");
    const vv = x6el("div", "v " + (c || ""), v);
    if (small) vv.append(x6el("small", null, small));
    s.append(x6el("div", "k", k), vv);
    box.append(s);
  }
}
// 狀態文字。失敗時「失敗」兩個字就是連到 log 的連結
function x6stateEl(tag, state, fail, word) {
  const box = x6el(tag, "x6-state" + (X6_BAD.includes(state) ? " bad" : ""));
  word = word || X6_LABEL[state];
  if (state !== "fail" || !fail || !fail.ev) { box.textContent = word; return box; }
  const b = x6el("button", "x6-faillink", word);
  b.type = "button";
  b.append(x6el("span", "lg", "log"));
  const after = fail.after.length ? `${fail.after.join("、")} 因為 ${fail.label} 失敗沒有執行` : "";
  b.dataset.tip = [`${fail.label} 失敗`, fail.err && `error: ${fail.err}`, after, "點一下看 log"].filter(Boolean).join("\n");
  b.setAttribute("aria-label", `${fail.label} 失敗,看 log`);
  b.addEventListener("click", () => x6openLog(fail.ev));
  box.append(b);
  return box;
}
function x6logBtn(ev, text) {
  const b = x6el("button", "x6-link", text || "查看 log");
  b.type = "button";
  b.addEventListener("click", () => x6openLog(ev));
  return b;
}

// ── 警示:分類上的數字與主頁籤的 ⚠ 都從這裡來 ──
function x6systems(view) {
  return ((x6Snap && x6Snap.status && x6Snap.status.systems) || []).filter(s => s.view === view && s.configured);
}
function x6todayPeriod(sys) {
  return (sys.periods || []).find(p => p.key === x6Snap.status.date);
}
function x6periodCols(v) {
  const today = x6Snap.status.date;
  const vols = x6trackVols(v, today);
  const min = Math.min(...vols);
  const extra = new Set();
  for (const s of x6systems(v)) {
    if (s.period === "none") continue;
    for (const p of s.periods || []) if (+p.key > min && !vols.includes(+p.key)) extra.add(+p.key);
  }
  return [...vols, ...extra].sort((a, b) => b - a).map(vol => ({ vol, iso: x6volDate(v, vol) }));
}
function x6older(v, sys) {
  if (sys.period === "none") return [];
  const min = Math.min(...x6periodCols(v).map(c => c.vol));
  return (sys.periods || []).filter(p => +p.key < min && p.last_at);
}
function x6engSpan(p) {
  for (const s of Object.values(p.steps || {})) {
    const d = (s.event && s.event.data) || {};
    const f = d.FILE_I || d.FILE || "";
    const m = f.match(/_(\d{4})_to_\d+_(\d{4})_/);
    if (m) return { lo: +m[1], hi: +m[2] };
  }
  return null;
}
function x6alerts() {
  const out = { daily: 0, twa: 0, twb: 0, adhoc: 0 };
  if (!x6Snap || !x6Snap.status) return out;
  for (const s of x6systems("daily")) {
    const p = x6todayPeriod(s);
    if (p && X6_BAD.includes(p.state)) out.daily++;
  }
  for (const v of ["twa", "twb"]) {
    const vols = x6periodCols(v).map(c => String(c.vol));
    for (const s of x6systems(v)) {
      for (const p of s.periods || []) {
        if (!X6_BAD.includes(p.state)) continue;
        if (s.period === "none" || vols.includes(p.key) || x6older(v, s).includes(p)) out[v]++;
      }
    }
  }
  for (const s of x6Snap.status.systems || []) if (s.kind === "adhoc") out.adhoc += s.alerts || 0;
  return out;
}
function x6connProblem() {
  if (!x6Snap) return false;
  if (x6Snap.ok === false) return true;
  const w = x6Snap.wiki || {};
  // unknown = 伺服器剛啟動、第一次抓信還沒完成:卡片顯示灰色「還沒抓信」,不是問題
  const gmailBad = x6Snap.auth !== "ok" && x6Snap.auth !== "unknown";
  return gmailBad || !!x6Snap.last_error || !!(w.last_login && !w.last_login.ok);
}

// ── 畫面 ──
function x6render() {
  if (!x6$("panel-xfer690")) return;
  const a = x6alerts();
  x6renderToolbar();
  x6renderNav(a);
  for (const v of ["daily", "period", "adhoc", "wiki"]) x6$("x6-v-" + v).hidden = true;
  if (x6Snap && x6Snap.status) {
    if (x6View === "daily") x6renderDaily();
    else if (x6View === "adhoc") x6renderAdhoc();
    else if (x6View === "wiki") x6renderWiki();
    else x6renderPeriod(x6View);
  }
  x6renderConn();
  x6setWarn("tab-xfer690", a.daily + a.twa + a.twb + a.adhoc > 0);
  x6setWarn("tab-xfer690conn", x6connProblem());
}
function x6setWarn(tabId, on) {
  const b = x6$(tabId);
  if (!b) return;
  let w = b.querySelector(".x6-warn");
  if (!w) { w = x6el("span", "x6-warn", "⚠"); w.setAttribute("aria-label", "有狀況"); b.append(w); }
  w.hidden = !on;
}

function x6renderToolbar() {
  const u = x6$("x6-upd");
  u.replaceChildren();
  if (!x6Snap) { u.textContent = "讀取中…"; return; }
  if (x6Snap.ok === false) { u.append(x6el("span", "x6-bad", x6Snap.message || "690 監控沒有啟動")); return; }
  if (x6Snap.last_poll) {
    const kind = x6Snap.last_poll_kind === "manual" ? "手動" : "自動";
    u.append("最後更新 ", x6el("b", null, `${x6Snap.last_poll.slice(5, 10)} ${x6Snap.last_poll.slice(11, 19)}`), `(${kind})`);
  } else {
    u.append("還沒抓過信");
  }
  if (x6Snap.last_error) {
    u.append(x6el("span", "x6-bad", ` · ${x6Snap.last_poll ? "之後抓信都失敗:" : ""}${x6Snap.last_error}`));
  } else if (x6Snap.stale && x6Snap.last_poll) {
    u.append(x6el("span", "x6-bad", " · 資料過舊"));
  } else {
    u.append(` · 每 ${x6Snap.poll_interval_min || 10} 分鐘自動更新`);
  }
}

function x6renderNav(a) {
  document.querySelectorAll("#panel-xfer690 .x6-sn").forEach(b => {
    const v = b.dataset.v;
    b.setAttribute("aria-pressed", String(v === x6View));
    const badge = b.querySelector(".x6-badge");
    if (v === "wiki") {
      // 有表沒處理(頁面表頭被改過等):不能只顯示「沒有要登記的」,徽章改成紅色驚嘆號
      const broken = x6wikiTables().filter(t => t.error).length;
      const cnt = x6wikiCounts().rows;
      badge.hidden = !cnt && !broken;
      badge.textContent = broken ? "!" : (cnt || "");
      badge.classList.toggle("x6-info", !broken);
      badge.classList.toggle("bad", !!broken);
      badge.setAttribute("aria-label", broken ? `${broken} 張表沒有處理` : `${cnt} 列要登記`);
      return;
    }
    const cnt = a[v];
    badge.hidden = !cnt;
    badge.textContent = cnt || "";
    badge.setAttribute("aria-label", `${cnt} 件需要處理`);
  });
}

// ── 每日資轉:只有今天 ──
function x6renderDaily() {
  x6$("x6-v-daily").hidden = false;
  const today = x6Snap.status.date;
  x6$("x6-today").textContent = `TW 每日資轉 · ${today}`;
  const box = x6$("x6-systems");
  box.replaceChildren();
  const systems = x6systems("daily").slice().sort((p, q) => (p.expect_by || "").localeCompare(q.expect_by || "") || p.id.localeCompare(q.id));
  const counts = { ok: 0, nodata: 0, fail: 0, missing: 0, pending: 0, unknown: 0, none: 0 };
  for (const s of systems) {
    const p = x6todayPeriod(s) || { key: today, state: "none", steps: {} };
    counts[p.state] = (counts[p.state] || 0) + 1;
    const fail = p.state === "fail" ? x6failInfo(s, p) : null;
    const row = x6el("div", "x6-sys");
    row.append(x6el("i", "x6-dot " + p.state));
    const nm = x6el("div", "name", s.label);
    nm.append(x6el("small", null, (s.auto || []).join(" · ")));
    row.append(nm, x6stateEl("div", p.state, fail));
    const steps = x6el("div", "x6-steps");
    for (const pr of s.pairs || []) {
      const st = x6pairState(p, pr);
      const lab = x6stepLabel(s, pr[0]);
      const at = ((p.steps || {})[pr[1] || pr[0]] || (p.steps || {})[pr[0]] || {}).at;
      const sp = x6el("span", "x6-step" + (st === "fail" ? " failed" : ""));
      sp.append(x6dot(st, `${s.label} · ${lab}(${pr.filter(Boolean).join(" / ")})\n${X6_LABEL[st]}${at ? `,${x6hm(at)} 收到` : ""}`),
                document.createTextNode(lab));
      steps.append(sp);
    }
    row.append(steps, x6el("div", "when", p.last_at ? `收信 ${x6hm(p.last_at)}` : `最晚 ${s.expect_by}`));
    const box2 = x6explain(s, p);
    if (box2) row.append(box2);
    box.append(row);
  }
  const done = counts.ok + counts.nodata;
  const bad = counts.fail + counts.missing + counts.unknown;
  x6stats(x6$("x6-d-summary"), [
    ["完成", `${done} / ${systems.length}`, done === systems.length ? "good" : ""],
    ["需要處理", String(bad), bad ? "bad" : "good"],
    ["今天還沒跑到", String(counts.pending), ""],
  ]);
}
// 無資料與該到沒到的說明框(失敗不出框,錯誤訊息在「失敗」連結的提示裡)
function x6explain(s, p) {
  if (p.state === "nodata") {
    const ev = x6nodataEvent(p);
    const de = x6el("div", "x6-detail soft");
    if (ev && ev.error) de.append(x6el("code", null, ev.error));
    if (ev) de.append(x6logBtn(ev));
    de.append(x6el("span", null, s.period === "date" ? "當天沒有新檔,視為完成,wiki 照樣打 v" : "這期的檔案還沒到"));
    return de;
  }
  if (p.state === "missing") {
    const de = x6el("div", "x6-detail bad");
    if (!p.last_at) {
      de.append(x6el("span", null, `過了 ${s.expect_by} 還沒收到信`));
    } else {
      const got = (s.pairs || []).filter(pr => x6pairState(p, pr) === "ok").map(pr => x6stepLabel(s, pr[0]));
      de.append(x6el("span", null, `只收到 ${got.join("、") || "部分步驟"},之後的步驟${Object.keys(p.auto || {}).length ? "" : "和母 job"}都沒有來信,也沒有失敗信。`));
    }
    return de;
  }
  return null;
}

// ── TWA / TWB:資料種類 × 公告期數 ──
const x6splitSel = (k) => { const i = k.indexOf("|"); return [k.slice(0, i), k.slice(i + 1)]; };
const x6isSel = (k) => { const [v, rest] = x6splitSel(k); return x6Sel[v] === rest; };
function x6sysById(id) { return (x6Snap.status.systems || []).find(s => s.id === id); }

function x6renderPeriod(v) {
  x6$("x6-v-period").hidden = false;
  const cols = x6periodCols(v);
  const systems = X6_TYPES.map(([t, name]) => [x6systems(v).find(s => s.type === t), name]).filter(x => x[0]);
  x6$("x6-p-title").textContent = `TW · ${X6_VIEWS[v]}`;
  const a = x6alerts()[v];
  x6stats(x6$("x6-p-summary"), [
    ["最新一期", `VOL ${cols[0].vol}`, "", x6md(cols[0].iso)],
    ["需要處理", String(a), a ? "bad" : "good"],
    ["Wiki 要登記", String(x6wikiCounts(v).rows), "", "列"],
  ]);

  // 預設選有問題的那一格,沒有就選最近收到信的
  const valid = (key) => { if (!key) return false; const [id, k] = x6splitSel(key); const s = x6sysById(id); return s && (s.periods || []).some(p => p.key === k); };
  if (!valid(x6Sel[v])) {
    let best = null, bad = null;
    for (const [s] of systems) for (const p of s.periods || []) {
      if (!p.last_at) continue;
      const key = `${s.id}|${p.key}`;
      if (X6_BAD.includes(p.state) && !bad) bad = key;
      if (!best || p.last_at > best.at) best = { at: p.last_at, key };
    }
    x6Sel[v] = bad || (best && best.key) || null;
  }

  const hasOlder = systems.some(([s]) => x6older(v, s).length || (s.period === "none" && (s.periods || []).some(p => !x6spanIn(p, cols))));
  const tbl = x6$("x6-p-table");
  tbl.replaceChildren();
  const thead = x6el("thead"), hr = x6el("tr");
  hr.append(x6el("th", null, "資料種類"));
  cols.forEach((c, i) => {
    const th = x6el("th", "c vol", String(c.vol));
    th.append(x6el("small", null, x6md(c.iso)));
    hr.append(th);
  });
  if (hasOlder) {
    const th = x6el("th", null, "更早的期");
    th.append(x6el("small", "sub", "近一個月內有補跑"));
    hr.append(th);
  }
  thead.append(hr);
  tbl.append(thead);
  const tb = x6el("tbody");
  for (const [s, name] of systems) {
    const tr = x6el("tr");
    const nm = x6el("td", "type");
    const sched = s.schedule === "manual" ? "不定期" : s.schedule === "daily" ? "每天" :
                  `每月 ${((s.schedule || {}).monthdays || []).join("、")} 日`;
    nm.append(x6el("b", null, name), x6el("small", null, `${(s.auto || []).join(" · ")} · ${sched}`));
    tr.append(nm);
    const used = new Set();
    for (let i = 0; i < cols.length; i++) {
      const col = cols[i];
      const td = x6el("td", "c");
      if (s.period === "none") {
        const p = (s.periods || []).find(q => { const sp = x6engSpan(q); return sp && sp.hi === col.vol; });
        if (p) {
          const sp = x6engSpan(p);
          const w = Math.min(sp.hi - sp.lo + 1, cols.length - i);
          td.colSpan = w;
          const key = `${v}|${s.id}|${p.key}`;
          const b = x6el("button", "x6-chip");
          b.type = "button";
          b.dataset.sel = key;
          b.dataset.tip = `${name} · VOL ${sp.lo}–${sp.hi}\n${X6_LABEL[p.state]},${x6mdhm(p.last_at)} 收到`;
          b.setAttribute("aria-pressed", String(x6isSel(key)));
          b.append(x6el("i", "x6-dot " + p.state), document.createTextNode(`${sp.lo}–${sp.hi} ${p.state === "nodata" ? "無檔案" : X6_LABEL[p.state]}`));
          if (x6isSel(key)) td.classList.add("on");
          td.append(b);
          tr.append(td);
          used.add(p.key);
          i += w - 1;
          continue;
        }
        td.append(x6dot("none", `${name} · VOL ${col.vol}\n不定期,這期沒有執行過`));
        tr.append(td);
        continue;
      }
      const p = (s.periods || []).find(q => q.key === String(col.vol));
      const head = `${v.toUpperCase()} ${name} · VOL ${col.vol}(${col.iso} 那期)`;
      if (!p) {
        const why = s.schedule === "manual" ? (s.periods || []).length ? "不定期,這期沒有執行過" : "還沒收到過這個系統的信"
                                             : "這期沒有紀錄";
        td.append(x6dot("none", `${head}\n${why}`));
      } else {
        const key = `${v}|${s.id}|${p.key}`;
        const lines = [head, p.last_at ? `${X6_LABEL[p.state]},${x6mdhm(p.last_at)} 收到` :
                                         p.state === "pending" ? `最晚 ${s.expect_by},還沒收到信` : `過了 ${s.expect_by} 還沒收到信`];
        const f = p.state === "fail" ? x6failInfo(s, p) : null;
        if (f) lines.push(`${f.label} 失敗`);
        const w = x6writes(s, p);
        if (w != null) lines.push(`寫入 ${x6num(w)} 筆`);
        if (x6runs(p) > 1) lines.push(`共執行 ${x6runs(p)} 次,以最後一次為準`);
        td.append(x6dot(p.state, lines.join("\n"), p.last_at ? key : null));
        if (x6isSel(key)) td.classList.add("on");
      }
      tr.append(td);
    }
    if (hasOlder) {
      const td = x6el("td");
      const box = x6el("div", "x6-olders");
      const olders = s.period === "none" ? (s.periods || []).filter(p => !used.has(p.key)) : x6older(v, s);
      for (const p of olders) {
        const key = `${v}|${s.id}|${p.key}`;
        const sp = s.period === "none" ? x6engSpan(p) : null;
        const label = sp ? `${sp.lo}–${sp.hi}` : s.period === "none" ? x6mdhm(p.first_at) : p.key;
        const b = x6el("button", "x6-chip");
        b.type = "button";
        b.dataset.sel = key;
        b.dataset.tip = `${name} · ${label}${p.date && s.period !== "none" ? `(${p.date} 那期)` : ""}\n${X6_LABEL[p.state]},${x6mdhm(p.last_at)} 收到`;
        b.setAttribute("aria-pressed", String(x6isSel(key)));
        b.append(x6el("i", "x6-dot " + p.state), document.createTextNode(label));
        box.append(b);
      }
      td.append(box);
      tr.append(td);
    }
    tb.append(tr);
  }
  tbl.append(tb);
  x6renderDetail(v, cols);
}
function x6spanIn(p, cols) {
  const sp = x6engSpan(p);
  return !!sp && cols.some(c => c.vol === sp.hi);
}

function x6renderDetail(v, cols) {
  const box = x6$("x6-pd");
  box.replaceChildren();
  if (!x6Sel[v]) { box.append(x6el("p", "hint", "點表格裡的圓點,看那一期的細節")); return; }
  const [id, k] = x6splitSel(x6Sel[v]);
  const s = x6sysById(id);
  const p = s && (s.periods || []).find(q => q.key === k);
  if (!p) return;
  const name = (X6_TYPES.find(t => t[0] === s.type) || [, s.label])[1];
  const sp = s.period === "none" ? x6engSpan(p) : null;
  const what = sp ? `VOL ${sp.lo}–${sp.hi}` : s.period === "none" ? x6mdhm(p.first_at) : `VOL ${p.key}`;
  const fail = p.state === "fail" ? x6failInfo(s, p) : null;
  box.append(x6el("div", "x6-pd-cap", "選取那一格的細節"));
  const head = x6el("div", "x6-pd-head");
  head.append(x6el("i", "x6-dot " + p.state), x6el("b", null, `${v.toUpperCase()} ${name} · ${what}`),
              x6stateEl("span", p.state, fail, p.state === "nodata" ? "無檔案" : null));
  box.append(head);
  const steps = x6el("div", "x6-steps");
  for (const pr of s.pairs || []) {
    const st = x6pairState(p, pr);
    const lab = x6stepLabel(s, pr[0]);
    const sp2 = x6el("span", "x6-step" + (st === "fail" ? " failed" : ""));
    sp2.append(x6dot(st, `${name} · ${lab}(${pr.filter(Boolean).join(" / ")})\n${X6_LABEL[st]}`), document.createTextNode(lab));
    steps.append(sp2);
  }
  box.append(steps);
  const ex = x6explain(s, p);
  if (ex) box.append(ex);
  const kv = x6el("dl", "x6-kv");
  const add = (dt, dd) => kv.append(x6el("dt", null, dt), x6el("dd", null, dd));
  const oldest = Math.min(...cols.map(c => c.vol));
  if (s.period !== "none" && p.date) add("期別", `${p.date} 那期${+p.key < oldest ? "(超過一個月,是補跑的舊期)" : ""}`);
  if (sp) add("期別", `一次涵蓋 ${sp.hi - sp.lo + 1} 期`);
  if (p.first_at) add("收信", p.first_at.slice(0, 10) === (p.last_at || "").slice(0, 10)
                           ? `${x6mdhm(p.first_at)}–${x6hm(p.last_at)}` : `${x6mdhm(p.first_at)} ~ ${x6mdhm(p.last_at)}`);
  const w = x6writes(s, p);
  if (w != null) add("寫入", `${x6num(w)} 筆`);
  if (p.tag) add("資料包 TAG", p.tag);
  if (x6runs(p) > 1) add("執行次數", `${x6runs(p)} 次,以最後一次為準`);
  add("代碼", `${(s.auto || []).join(" · ")}${(s.groups || []).length ? " · " + s.groups.join("、") : ""}`);
  box.append(kv);
}

// ── 其他作業 ──
function x6renderAdhoc() {
  x6$("x6-v-adhoc").hidden = false;
  const all = (x6Snap.status.systems || []);
  const runs = [];
  for (const s of all.filter(x => x.kind === "adhoc")) for (const p of s.periods || []) runs.push([s, p]);
  runs.sort((a, b) => (b[1].last_at || "").localeCompare(a[1].last_at || ""));
  const maint = all.filter(x => x.kind === "maint");
  const nFail = runs.filter(([, p]) => p.state === "fail").length;
  const mBad = maint.filter(s => (s.periods[0] || {}).state === "fail").length;
  x6stats(x6$("x6-a-summary"), [
    ["不在清單的作業", String(runs.length), "", "次"],
    ["失敗", String(nFail), nFail ? "bad" : "good"],
    ["維運", `${maint.length} 項`, mBad ? "bad" : "good", mBad ? `${mBad} 項失敗` : "都正常"],
  ]);
  const box = x6$("x6-adhoc");
  box.replaceChildren();
  if (!runs.length) box.append(x6el("p", "hint", "近 7 天沒有清單以外的作業。"));
  for (const [s, p] of runs) {
    const codes = Object.keys(p.steps || {}).sort();
    const first = (s.names || {})[codes[0]] || "";
    const job = first.replace(/-step\d+-.*$|-auto$/, "") || s.id;
    const row = x6el("div", "x6-sys wide");
    row.append(x6el("i", "x6-dot " + p.state));
    const nm = x6el("div", "name", job);
    nm.append(x6el("small", null, `${s.id} · ${(s.groups || []).join("、")}`));
    let fail = null;
    if (p.state === "fail") {
      const fc = codes.find(c => p.steps[c].state === "fail");
      const ev = fc && p.steps[fc].event;
      fail = { label: ((s.names || {})[fc] || fc).replace(job + "-", ""), ev, err: (ev && ev.error) || "", after: [] };
    }
    row.append(nm, x6stateEl("div", p.state, fail));
    const steps = x6el("div", "x6-steps");
    for (const c of codes) {
      const st = p.steps[c].state;
      const lab = ((s.names || {})[c] || c).replace(job + "-", "");
      const sp = x6el("span", "x6-step" + (st === "fail" ? " failed" : ""));
      sp.append(x6dot(st, `${c}-${(s.names || {})[c] || ""}\n${X6_LABEL[st] || st}`), document.createTextNode(lab));
      steps.append(sp);
    }
    row.append(steps, x6el("div", "when", `收信 ${x6mdhm(p.last_at)}`));
    const info = x6el("div", "x6-detail soft");
    const evs = codes.map(c => p.steps[c].event).filter(Boolean);
    const data = Object.assign({}, ...evs.map(e => e.data || {}));
    const dtxt = Object.entries(data).map(([k, v]) => `${k}=${v}`).join(" · ");
    if (dtxt) info.append(x6el("code", null, dtxt));
    const w = evs.map(e => e.writes).filter(x => x != null);
    if (w.length) info.append(x6el("span", null, `寫入 ${x6num(w[w.length - 1])} 筆`));
    info.append(x6el("span", null, "不在系統清單裡,只顯示、不寫 Wiki"));
    row.append(info);
    box.append(row);
  }
  const m = x6$("x6-maint");
  m.replaceChildren();
  for (const s of maint) {
    const p = s.periods[0];
    if (!p) continue;
    const name = (s.names || {})[s.id] || "";
    const act = x6el("span");
    const ev = ((p.steps || {})[s.id] || {}).event;
    m.append(x6el("span", "t", x6mdhm(p.last_at)), x6el("i", "x6-dot " + p.state),
             x6el("span", "s", `${s.id}-${name}`), p.state === "fail" && ev ? x6logBtn(ev) : act);
  }
}

// ── Wiki 登記 ──
function x6wikiTables() {
  return ((x6Snap && x6Snap.wiki && x6Snap.wiki.tables) || []);
}
const x6ek = (tb, row, col) => `${tb.page}|${tb.table}|${row.key}|${col}`;
// 一格要怎麼顯示:{v, k(fill/old/hold/wait/edit), was, flag, tip, existing}
function x6cell(tb, row, c) {
  const raw = (row.row || [])[c.col] || "";
  const cell = (row.cells || {})[c.col];
  let out;
  if (!cell) out = { v: raw, k: row.new ? "fill" : "old" };
  else if (cell.state === "fill") out = { v: cell.value, k: "fill", was: cell.existing || "", existing: cell.existing || "" };
  else if (cell.state === "hold") out = { v: "留空", k: "hold", tip: "失敗或沒到,這格不填", existing: cell.existing || "" };
  else if (cell.state === "wait") out = { v: "待跑", k: "wait", tip: "還沒跑到,跑完以後再填", existing: cell.existing || "" };
  else if (cell.state === "conflict") {
    const what = c.kind === "writes" ? "L1 寫入筆數" : "log";
    out = { v: cell.existing, k: "old", flag: true, existing: cell.existing,
            tip: `頁面上寫 ${cell.existing},${what}是 ${cell.value}。程式不會自動改人填的值,要改就點這格` };
  } else {
    out = { v: cell.existing != null ? cell.existing : raw, k: "old", existing: cell.existing != null ? cell.existing : raw };
    if (c.kind === "writes" && cell.state === "same") out.tip = `L1 寫入筆數是 ${cell.value},跟頁面上一致`;
  }
  if (out.existing == null) out.existing = raw;
  // auto = 不手改的話,寫入後這格會是什麼(要填的就是填的值,其他照頁面原值)
  out.auto = out.k === "fill" ? out.v : out.existing;
  const ek = x6ek(tb, row, c.col);
  if (ek in x6Edits) {
    const was = out.k === "hold" || out.k === "wait" ? out.existing : (out.k === "fill" ? out.existing : out.v);
    out = { v: x6Edits[ek].value, k: "edit", was, existing: out.existing, auto: out.auto,
            overwrite: !!out.existing && !/^\d{4}x{4}$/.test(out.existing),
            tip: `你改的,原本 ${was || "(空白)"}`
                 + (out.existing && !/^\d{4}x{4}$/.test(out.existing) ? `\n會覆寫頁面上人填的 ${out.existing};寫入前會先確認頁面上還是這個值` : "") };
  }
  return out;
}
function x6wikiCounts(view) {
  let rows = 0, fill = 0, hold = 0, added = 0, edits = 0;
  const pages = new Set();
  for (const tb of x6wikiTables()) {
    if (view && !tb.table.toLowerCase().startsWith(view)) continue;
    for (const row of tb.rows || []) {
      let f = 0, e = 0;
      for (const c of tb.columns) {
        const x = x6cell(tb, row, c);
        if (x.k === "fill" && c.kind !== "key" && c.kind !== "info") f++;
        if (x.k === "hold") hold++;
        if (x.k === "edit") e++;
      }
      if (f || e) { rows++; pages.add(tb.page); }
      if (row.new) added++;
      fill += f; edits += e;
    }
  }
  return { rows, fill, hold, added, edits, pages: pages.size };
}
function x6renderWiki() {
  x6$("x6-v-wiki").hidden = false;
  const w = (x6Snap && x6Snap.wiki) || {};
  const c = x6wikiCounts();
  x6stats(x6$("x6-w-summary"), [
    ["會改的頁", String(c.pages), "", "頁"],
    ["要填的格", String(c.fill), "", "格"],
    ["新增的列", String(c.added), "", "列"],
    ["失敗留空", String(c.hold), c.hold ? "bad" : "good", "格"],
  ]);
  const go = x6$("x6-w-go");
  if (!x6Busy.write) {
    go.removeAttribute("aria-busy");
    go.textContent = !w.logged_in ? (w.last_login && !w.last_login.ok ? "Wiki 登入失敗" : "Wiki 還沒登入")
                   : !c.pages ? "沒有要寫入的"
                   : `寫入 Wiki(${c.pages} 頁${c.edits ? `,含手改 ${c.edits} 格` : ""})${w.dry_run ? " · 乾跑" : ""}`;
    go.disabled = !!w.logged_in && !c.pages;
  }
  const res = x6$("x6-w-result");
  res.hidden = !x6Result;
  if (x6Result) { res.className = "x6-result " + (x6Result.ok ? "ok" : "bad"); res.textContent = x6Result.text; }
  x6renderEdits();

  const box = x6$("x6-wikipages");
  box.replaceChildren();
  if (!w.logged_in && !x6wikiTables().length) {
    box.append(x6el("div", "x6-wnote", w.enabled ? "到「信件與連線」登入 Wiki 之後,這裡會讀出各頁目前的內容,顯示填好的樣子。"
                                                 : "Wiki 登記還沒啟用(data/config/xfer690.json 的 wiki.enabled)。"));
    return;
  }
  if (w.last_error) box.append(x6el("div", "x6-result bad", w.last_error));
  const order = ["每日執行項目", "TW全文Full", "TW書目Biblio及雜項"];
  const tables = x6wikiTables().slice().sort((a, b) => order.indexOf(a.page) - order.indexOf(b.page) || a.table.localeCompare(b.table));
  for (const tb of tables) {
    const sec = x6el("div", "x6-wpage");
    sec.append(x6el("h3", null, tb.title));
    if (tb.error) sec.append(x6el("div", "x6-result bad",
      `這張表沒有處理,不會寫入:${tb.error}。頁面的表格改過的話,要更新 data/config/xfer690.json 這張表的 labels / cols。`));
    const rows = (tb.rows || []).filter(r => tb.columns.some(c => {
      const x = x6cell(tb, r, c);
      return ["fill", "hold", "wait", "edit"].includes(x.k) && c.kind !== "key" || x.flag;
    }));
    if (rows.length) sec.append(x6wtable(tb, rows));
    const notes = [];
    if (!rows.length && !tb.error) notes.push(tb.table === "TWB" && tb.page === "TW全文Full" && !(tb.rows || []).length
                                              ? "還沒收到過全文 TWB 的信,沒有要登記的" : "沒有要登記的");
    if (notes.length) sec.append(x6el("div", "x6-wnote", notes.join(";") + "。"));
    box.append(sec);
  }
  const off = x6el("div", "x6-wpage");
  off.append(x6el("div", "x6-wnote", "TW英譯En、TW影像GCS 這版不寫入。"));
  box.append(off);
}
function x6wtable(tb, rows) {
  const wrap = x6el("div", "x6-wrap");
  const t = x6el("table", "x6-wt");
  const thead = x6el("thead");
  const daily = tb.columns.some(c => c.system) && !tb.table;
  if (daily) {
    const r1 = x6el("tr"), r2 = x6el("tr");
    let last = null;
    for (const c of tb.columns) {
      if (!c.system) { const th = x6el("th", null, c.kind === "key" ? "日期" : c.label); th.rowSpan = 2; r1.append(th); continue; }
      if (c.group !== last) { const g = x6el("th", "grp", c.group); g.colSpan = tb.columns.filter(x => x.group === c.group).length; r1.append(g); last = c.group; }
      r2.append(x6el("th", c === tb.columns.find(x => x.group === c.group) ? "grp" : null, c.label));
    }
    thead.append(r1, r2);
  } else {
    const hr = x6el("tr");
    for (const c of tb.columns) {
      const th = x6el("th", null, c.label);
      if (c.kind === "writes") {
        th.append(x6el("span", "hint", " 筆數"));
        th.dataset.tip = `${c.label} = ${c.group} L1 的寫入筆數`;
        th.tabIndex = 0;
      }
      hr.append(th);
    }
    thead.append(hr);
  }
  t.append(thead);
  const body = x6el("tbody");
  for (const row of rows) {
    const tr = x6el("tr");
    for (const c of tb.columns) {
      if (c.kind === "key") {
        const td = x6el("td", "key", row.key);
        if (row.new) {
          td.append(x6el("span", "x6-tag add", "新增"));
          // 第一次正式寫入先試一列:只插這一列,好檢查、好還原
          const one = x6el("button", "x6-link x6-one", "只寫這一列");
          one.type = "button";
          one.disabled = x6Busy.write || !(x6Snap && x6Snap.wiki && x6Snap.wiki.logged_in);
          one.addEventListener("click", () => x6wikiWrite({ page: tb.page, table: tb.table, key: row.key, title: tb.title }));
          td.append(one);
        }
        tr.append(td);
        continue;
      }
      const x = x6cell(tb, row, c);
      const first = daily && c.system && c === tb.columns.find(y => y.group === c.group);
      const td = x6el("td", "cell-" + x.k + (first ? " grp" : "") + (x.flag ? " flag" : ""));
      const shown = x.k === "edit" && !x.v ? "(空白)" : x.v;
      if ((x.k === "edit" || x.k === "fill") && x.was && x.was !== x.v) td.append(x6el("s", null, x.was), document.createTextNode(shown));
      else td.textContent = shown;
      td.dataset.tip = [x.tip, "點一下可以修改"].filter(Boolean).join("\n");
      td.tabIndex = 0;
      td.dataset.ek = x6ek(tb, row, c.col);
      // 新增的列頁面上還沒有:原值是空白,改的值直接當成插入那列的內容
      td.dataset.expect = row.new ? "" : (x.existing || "");
      td.dataset.cur = x.k === "hold" || x.k === "wait" ? "" : x.v;
      td.dataset.auto = x.auto || "";
      td.dataset.label = c.label || String(c.col);
      td.dataset.system = c.system || "";
      td.dataset.page = tb.page;
      td.dataset.table = tb.table;
      td.dataset.key = row.key;
      tr.append(td);
    }
    body.append(tr);
  }
  t.append(body);
  wrap.append(t);
  return wrap;
}
// 改過的格本身有底色、刪除線和提示,這裡只放「全部還原」(有改才出現)
function x6renderEdits() {
  const n = Object.keys(x6Edits).length;
  const all = x6$("x6-w-undo");
  all.hidden = !n;
  all.textContent = `全部還原(${n} 格)`;
}
function x6startEdit(td) {
  if (td.querySelector("input")) return;
  const ek = td.dataset.ek, expect = td.dataset.expect;
  const inp = document.createElement("input");
  inp.value = td.dataset.cur;
  inp.placeholder = expect || "(空白)";
  inp.setAttribute("aria-label", "修改這一格,Enter 存、Esc 取消");
  td.replaceChildren(inp);
  x6$("x6-tip").hidden = true;
  inp.focus(); inp.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save) {
      const val = inp.value.trim();
      // 跟「不手改的結果」一樣才算沒改。跟頁面原值一樣不算:那是「維持原值、不要自動填」
      // (例如清掉程式要打的 v),要送出,自動填格才會跳過這格
      if (val === td.dataset.auto) delete x6Edits[ek];
      else if (val !== td.dataset.cur || ek in x6Edits) {
        x6Edits[ek] = { page: td.dataset.page, table: td.dataset.table, key: td.dataset.key,
                        col: td.dataset.label, system: td.dataset.system || undefined, expect, value: val };
      }
    }
    x6render();
    const again = document.querySelector(`td[data-ek="${CSS.escape(ek)}"]`);
    if (again) again.focus({ preventScroll: true });
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  inp.addEventListener("blur", () => finish(true));
}

// ── 信件與連線 ──
function x6renderConn() {
  const box = x6$("x6-conn");
  if (!box) return;
  box.replaceChildren();
  const s = x6Snap;
  if (!s) return;
  const card = (state, title, word, rows, fix, act) => {
    const c = x6el("div", "x6-cbox");
    const h = x6el("div", "x6-cbox-h");
    h.append(x6el("i", "x6-dot " + state), x6el("b", null, title), x6el("span", "x6-state" + (X6_BAD.includes(state) ? " bad" : ""), word));
    const kv = x6el("dl", "x6-kv");
    for (const [k, v] of rows) kv.append(x6el("dt", null, k), x6el("dd", null, v));
    c.append(h, kv);
    if (fix) c.append(x6el("div", "x6-fix", fix));
    if (act) c.append(act);
    return c;
  };
  if (s.ok === false) { box.append(card("fail", "690 監控", "沒有啟動", [], s.message)); return; }
  const AUTH = { ok: ["ok", "正常"], needs_auth: ["fail", "授權已失效"], missing_credentials: ["fail", "還沒設定"],
                 missing_package: ["fail", "缺少套件"], unknown: ["pending", "還沒抓信"] };
  let [gs, gw] = AUTH[s.auth] || ["unknown", s.auth];
  if (s.auth === "ok" && s.last_error) [gs, gw] = ["fail", "抓信失敗"];
  const rows = [];
  if (s.gmail_account) rows.push(["帳號", s.gmail_account]);
  rows.push(["最後抓信", s.last_poll ? `${x6mdhm(s.last_poll)}(${s.last_poll_kind === "manual" ? "手動" : "自動"})` : "還沒抓過"]);
  if (s.gmail_authorizing) [gs, gw] = ["pending", "等待授權"];
  else if (!s.gmail_authorized && s.auth !== "unknown") [gs, gw] = ["fail", s.auth === "missing_credentials" ? "還沒設定" : "還沒授權"];
  const fix = s.gmail_authorizing ? null
    : s.auth === "missing_credentials" ? s.last_error
    : !s.gmail_authorized ? `按下面的「${s.gmail_account || s.auth === "needs_auth" ? "重新授權" : "授權"}」,在瀏覽器用 Google 帳號同意唯讀權限。`
    : s.last_error;
  // 缺 GCP 用戶端檔時按鈕幫不上忙(要先照 690授權.bat 的說明設定),不顯示
  const act = s.auth === "missing_credentials" ? null : x6gmailBox(s);
  box.append(card(gs, "Gmail", gw, rows, gs === "ok" ? null : fix, act));

  const w = s.wiki || {};
  const ll = w.last_login;
  const state = w.logged_in ? "ok" : ll && !ll.ok ? "fail" : "pending";
  const word = w.logged_in ? "已登入" : ll && !ll.ok ? "登入失敗" : "還沒登入";
  const lw = w.last_write;
  const wrows = [];
  if (w.logged_in && w.account) wrows.push(["帳號", w.account]);
  wrows.push(["最後登入", ll ? `${x6mdhm(ll.at)} ${ll.ok ? "成功" : "失敗"}` : "—"]);
  wrows.push(["最後寫入", lw ? `${x6mdhm(lw.at)} · ${lw.dry_run ? "乾跑,沒有真的寫入;" : ""}${lw.pages} 頁,填 ${lw.filled} 格、新增 ${lw.inserted} 列${lw.edited ? `、手改 ${lw.edited} 格` : ""}` : "還沒寫入過"]);
  const wfix = !w.logged_in && ll && !ll.ok ? ll.message : null;
  box.append(card(state, "Wiki", word, wrows, wfix, x6wikiLoginBox(w)));

  const m = x6$("x6-mail");
  m.replaceChildren();
  const label = {};
  for (const sys of (s.status && s.status.systems) || []) {
    const cat = sys.kind === "maint" ? "維運" : sys.kind === "adhoc" ? "其他作業(不在清單)" :
                sys.view === "daily" ? `每日 · ${sys.label}` : `${(sys.view || "").toUpperCase()} · ${sys.label}`;
    for (const c of [...(sys.auto || []), ...(sys.steps || [])]) label[c] = cat;
  }
  if (!(s.recent || []).length) m.append(x6el("span", "hint", "還沒有信。"));
  for (const e of s.recent || []) {
    const d = x6el("span");
    d.append(x6el("i", "x6-dot " + (X6_LABEL[e.status] ? e.status : "unknown")));
    const sub = x6el("span", "s", `${e.code}-${e.name} `);
    sub.append(x6el("span", "hint", label[e.code] || ""));
    m.append(x6el("span", "t", x6mdhm(e.at)), d, sub,
             e.status === "fail" || e.status === "nodata" ? x6logBtn(e) : x6el("span"));
  }
}

// ── log 視窗 ──
async function x6openLog(ev) {
  const dlg = x6$("x6-logdlg");
  x6$("x6-log-title").textContent = `${ev.code}-${ev.name}.log`;
  x6$("x6-log-meta").textContent = `${x6mdhm(ev.at)}${ev.exec_id ? ` · 執行 #${ev.exec_id}` : ""}`;
  const body = x6$("x6-log-body");
  body.replaceChildren(x6el("div", null, "讀取中…(伺服器當下從 Gmail 抓這封信)"));
  x6$("x6-log-count").textContent = "";
  if (!dlg.open) dlg.showModal();
  try {
    const r = await fetch("/api/xfer690/log?id=" + encodeURIComponent(ev.id));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const text = await r.text();
    const lines = text ? text.replace(/\n$/, "").split("\n") : [];
    body.replaceChildren();
    let firstErr = null;
    lines.forEach((ln, i) => {
      const cls = /^\s*error:/.test(ln) ? "err" : /^RUNDECK:DATA:/.test(ln) ? "data" : /\[job_complete\]/.test(ln) ? "done" : null;
      const row = x6el("div", cls);
      row.append(x6el("i", null, String(i + 1)), document.createTextNode(ln));
      body.append(row);
      if (cls === "err" && !firstErr) firstErr = row;
    });
    if (!lines.length) body.append(x6el("div", null, "這封信沒有附 .log。"));
    x6$("x6-log-count").textContent = `${lines.length} 行`;
    if (firstErr) firstErr.scrollIntoView({ block: "center" });
  } catch (e) {
    body.replaceChildren(x6el("div", "err", `讀不到 log:${e.message}`));
  }
}

// ── 動作 ──
let x6Soon = null;
async function x6load() {
  try {
    const r = await fetch("/api/xfer690");
    x6Snap = await r.json();
  } catch (e) {
    x6Snap = x6Snap || { ok: false, message: "讀不到 /api/xfer690" };
  }
  x6render();
  // 第一次抓信還沒完成:每 3 秒再讀一次,抓完畫面馬上更新(平常每 60 秒)
  clearTimeout(x6Soon);
  // 等瀏覽器授權時也一樣:使用者按完同意,卡片馬上變成已授權
  if (x6Snap && x6Snap.ok !== false && (x6Snap.auth === "unknown" || x6Snap.gmail_authorizing))
    x6Soon = setTimeout(x6load, 3000);
}
async function x6refresh() {
  if (x6Busy.refresh) return;
  x6Busy.refresh = true;
  const b = x6$("x6-refresh");
  b.disabled = true; b.textContent = "抓信中…"; x6$("x6-spin").hidden = false;
  try {
    const r = await fetch("/api/xfer690/refresh", { method: "POST", headers: { "Content-Type": "application/json" },
                                                    body: "{}" });
    x6Snap = await r.json();
  } catch (e) {
    if (x6Snap) x6Snap.last_error = `手動更新失敗:${e.message}`;
  }
  x6Busy.refresh = false;
  b.disabled = false; b.textContent = "立即更新"; x6$("x6-spin").hidden = true;
  x6render();
  const u = x6$("x6-upd");
  u.classList.add("flash");
  setTimeout(() => u.classList.remove("flash"), 1500);
}
// Wiki 只開 Innovue OAuth(GitLab)登入,程式沒辦法自己登:使用者在瀏覽器登入後,
// 把 Cookies 裡的 jwt 貼過來。伺服器先跟 Wiki 確認有效才收下,畫面不回顯。
// 卡片每分鐘重畫一次,輸入框放在卡片外保留,重畫時才不會清掉貼到一半的內容。
const x6jwtInput = (() => {
  const i = document.createElement("input");
  i.type = "password"; i.id = "x6-jwt"; i.className = "x6-jwt";
  i.autocomplete = "off"; i.spellcheck = false;
  i.placeholder = "貼上 jwt 的值(eyJ 開頭)";
  i.addEventListener("keydown", (e) => { if (e.key === "Enter") x6wikiLogin(); });
  return i;
})();
function x6wikiLoginBox(w) {
  const wrap = x6el("div", "x6-login");
  if (w.logged_in) {
    const out = x6el("button", "x6-btn ghost", "登出");
    out.type = "button";
    out.disabled = x6Busy.write;
    out.addEventListener("click", x6wikiLogout);
    wrap.append(out);
    return wrap;
  }
  const ol = x6el("ol", "x6-howto");
  const li1 = x6el("li");
  const a = x6el("a", null, "Wiki");
  a.href = (w.url || "https://km.innovue.ltd") + "/login"; a.target = "_blank"; a.rel = "noopener";
  li1.append("開 ", a, ",用「Innovue OAuth」登入");
  ol.append(li1,
            x6el("li", null, "按 F12 → Application(應用程式)→ Cookies → 這個網站"),
            x6el("li", null, "點名稱是 jwt 的那一列,複製「值」,貼到下面"));
  const lab = x6el("label", "x6-jwt-lab", "jwt");
  lab.htmlFor = "x6-jwt";
  const btn = x6el("button", "x6-btn", x6Busy.login ? "確認中…" : "登入 Wiki");
  btn.type = "button";
  if (x6Busy.login) btn.setAttribute("aria-busy", "true");
  btn.disabled = x6Busy.login || x6Busy.write;
  x6jwtInput.disabled = x6Busy.login;
  btn.addEventListener("click", x6wikiLogin);
  const row = x6el("div", "x6-jwt-row");
  row.append(x6jwtInput, btn);
  wrap.append(ol, lab, row);
  return wrap;
}
async function x6wikiLogin() {
  if (x6Busy.login) return;
  const jwt = x6jwtInput.value.trim();
  if (!jwt) { x6jwtInput.focus(); return; }
  x6Busy.login = true;
  x6render();
  try {
    const r = await fetch("/api/xfer690/wiki/login", { method: "POST", headers: { "Content-Type": "application/json" },
                                                       body: JSON.stringify({ jwt }) });
    const j = await r.json();
    if (j.ok) x6jwtInput.value = "";
  } catch (e) { /* 結果看快照的 last_login */ }
  x6Busy.login = false;
  await x6load();
}
async function x6wikiLogout() {
  try {
    await fetch("/api/xfer690/wiki/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (e) { /* 下次載入看快照 */ }
  await x6load();
}

// ── Gmail 授權:沒授權顯示「授權」,已授權顯示「登出」(跟 Wiki 卡片一樣) ──
function x6gmailBox(s) {
  const wrap = x6el("div", "x6-login");
  const btn = x6el("button", "x6-btn" + (s.gmail_authorized ? " ghost" : ""));
  btn.type = "button";
  if (s.gmail_authorizing) {
    btn.textContent = "等待授權…";
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    wrap.append(btn, x6el("div", "hint", "已開啟瀏覽器:在 Google 頁面選帳號、按同意,完成後這裡會自動更新。"));
    return wrap;
  }
  if (s.gmail_authorized) {
    btn.textContent = "登出";
    btn.addEventListener("click", x6gmailLogout);
  } else {
    btn.textContent = s.gmail_account || s.auth === "needs_auth" ? "重新授權" : "授權";
    btn.addEventListener("click", x6gmailAuthorize);
  }
  wrap.append(btn);
  return wrap;
}
async function x6gmailAuthorize() {
  try {
    const r = await fetch("/api/xfer690/gmail/authorize", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const j = await r.json();
    if (!j.ok && x6Snap) x6Snap.last_error = j.message;
  } catch (e) { /* 下次載入看快照 */ }
  await x6load();
}
async function x6gmailLogout() {
  if (!confirm("登出 Gmail?\n這台電腦會停止抓信,直到重新授權。")) return;
  try {
    await fetch("/api/xfer690/gmail/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (e) { /* 下次載入看快照 */ }
  await x6load();
}
// only = {page, table, key, title}:「只寫這一列」,不送手改的格
async function x6wikiWrite(only) {
  const w = (x6Snap && x6Snap.wiki) || {};
  if (!w.logged_in) { showTab("xfer690conn"); return; }
  if (x6Busy.write) return;
  // 「只寫這一列」:那一列的手改一起送,別列的手改留著
  const mine = (e) => only && e.page === only.page && e.table === only.table && String(e.key) === String(only.key);
  const rowEdits = only ? Object.values(x6Edits).filter(mine) : [];
  if (only && !w.dry_run && !confirm(`只把「${only.title} ${only.key}」這一列寫進 Wiki?\n其他列、其他頁都不會動`
      + (rowEdits.length ? `;這一列你手改的 ${rowEdits.length} 格會一起寫入。` : "。"))) return;
  const go = x6$("x6-w-go");
  x6Busy.write = true; x6Result = null;
  go.disabled = true; go.textContent = "寫入中…"; go.setAttribute("aria-busy", "true"); x6$("x6-w-spin").hidden = false;
  x6render();
  const edits = only ? rowEdits : Object.values(x6Edits);
  const body = only ? { edits, only: { page: only.page, table: only.table, key: only.key } } : { edits };
  try {
    const r = await fetch("/api/xfer690/wiki/write", { method: "POST", headers: { "Content-Type": "application/json" },
                                                       body: JSON.stringify(body) });
    const j = await r.json();
    if (r.status === 409) {
      x6Result = { ok: false, text: "Wiki 登入已失效,請到「信件與連線」重新登入。修改都還在。" };
    } else if (!j.ok) {
      x6Result = { ok: false, text: j.message || "寫入失敗。修改都還在。" };
    } else {
      const pages = Object.values(j.pages || {});
      const sum = (k) => pages.reduce((a, p) => a + (p[k] || 0), 0);
      const conf = pages.flatMap(p => (p.conflicts || []).filter(c => c.reason === "頁面上的值已經被改過"));
      const errs = pages.flatMap(p => p.errors || []);
      const scope = only ? `「${only.title} ${only.key}」這一列` : `${pages.filter(p => p.updated).length} 頁`;
      const head = j.dry_run ? `乾跑模式(xfer690.json 的 wiki.dry_run),${only ? scope : ""}沒有真的寫入;` : `已寫入 ${scope}:`;
      let text = `${head}填 ${sum("filled")} 格、新增 ${sum("inserted")} 列${sum("edited") ? `、手改 ${sum("edited")} 格` : ""}。`;
      if (conf.length) text += ` ${conf.length} 格沒有改:頁面上的值已經被別人改過,請重新看過。`;
      if (errs.length) text += ` 有錯誤:${errs.join(";")}`;
      x6Result = { ok: !errs.length, text };
      if (!j.dry_run) {
        // 衝突的格留著讓使用者重看;其他送出的已經寫進去了。只寫一列時,別列的手改不動
        const keep = {};
        for (const [ek, e] of Object.entries(x6Edits)) {
          if (!(!only || mine(e)) || conf.some(c => String(e.key) === String(c.key))) keep[ek] = e;
        }
        x6Edits = keep;
      }
    }
  } catch (e) {
    x6Result = { ok: false, text: `寫入失敗:${e.message}。修改都還在。` };
  }
  x6Busy.write = false; x6$("x6-w-spin").hidden = true;
  await x6load();
}

// ── 提示框:滑鼠移到 / focus 有 data-tip 的元素 ──
function x6showTip(e) {
  const t = e.target.closest && e.target.closest(".x6 [data-tip]");
  const tip = x6$("x6-tip");
  if (!t || !tip) { if (tip) tip.hidden = true; return; }
  tip.textContent = t.dataset.tip;
  tip.hidden = false;
  const r = t.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
  let y = r.bottom + 8;
  if (y + h > window.innerHeight - 8) y = r.top - h - 8;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}

function x6bind() {
  document.querySelectorAll("#panel-xfer690 .x6-sn").forEach(b => b.addEventListener("click", () => {
    x6View = b.dataset.v;
    x6$("x6-tip").hidden = true;
    x6render();
  }));
  x6$("x6-refresh").addEventListener("click", x6refresh);
  x6$("x6-w-go").addEventListener("click", () => x6wikiWrite());
  x6$("x6-w-undo").addEventListener("click", () => { x6Edits = {}; x6render(); });
  x6$("x6-log-close").addEventListener("click", () => x6$("x6-logdlg").close());
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("#panel-xfer690 [data-sel]");
    if (b) {
      const key = b.dataset.sel;
      const [v, rest] = x6splitSel(key);
      x6Sel[v] = rest;
      x6render();
      const again = document.querySelector(`#panel-xfer690 [data-sel="${CSS.escape(key)}"]`);
      if (again) again.focus({ preventScroll: true });
      return;
    }
    const td = e.target.closest && e.target.closest("#panel-xfer690 td[data-ek]");
    if (td && !x6Busy.write) x6startEdit(td);
  });
  document.addEventListener("keydown", (e) => {
    const td = e.target.closest && e.target.closest("#panel-xfer690 td[data-ek]");
    if (td && e.key === "Enter" && !td.querySelector("input")) { e.preventDefault(); x6startEdit(td); }
  });
  for (const ev of ["mouseover", "focusin"]) document.addEventListener(ev, x6showTip);
  document.addEventListener("mouseout", (e) => { if (e.target.closest && e.target.closest(".x6 [data-tip]")) x6$("x6-tip").hidden = true; });
  document.addEventListener("focusout", () => { const t = x6$("x6-tip"); if (t) t.hidden = true; });
}

registerTab({
  id: "xfer690",
  label: "690資轉",
  order: 50,
  onInit() {
    x6bind();
    x6load();
    setInterval(x6load, 60000);
  },
  onShow() { x6load(); },
});
registerTab({
  id: "xfer690conn",
  label: "信件與連線",
  order: 51,
  onShow() { x6load(); },
});
