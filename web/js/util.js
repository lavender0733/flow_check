// ===== 共用工具與常數 =====
// 所有分頁共用的格式化函式與對照表。最先載入,其他模組執行時都能用到。
// (採傳統 <script>,所有檔共用同一全域作用域 → 函式與 let/const 跨檔可直接呼叫,
//  HTML 裡的 onclick="..." 也照常運作,不需打包工具。)

function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i >= 2 ? 2 : 0) + " " + u[i];
}
function fmtRate(bps) { return fmtBytes(bps) + "/s"; }

function fmtDur(sec) {
  sec = Math.round(sec || 0);
  if (!sec) return "0s";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function fmtMD(s) {
  s = String(s || "");
  if (s.length === 8) return s.slice(4, 6) + "/" + s.slice(6, 8);   // YYYYMMDD
  if (s.length === 6) return s.slice(4, 6) + "月";                   // YYYYMM
  return s || "—";
}

const STATUS_MAP = {
  idle:    ["閒置", "idle"],
  running: ["執行中", "run"],
  done:    ["完成", "done"],
  stopped: ["已停止", "stop"],
};
const MODE_MAP = { count: "指定筆數", budget: "流量自動停" };
const METHOD_MAP = { api: "API 檢索", web: "傳統檢索" };
