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
const METHOD_MAP = { api: "API 檢索", web: "傳統檢索" };

// ===== 頁籤註冊表 =====
// 每個頁籤模組在自己的檔尾呼叫 registerTab 把自己登記進來,main.js 再依 order
// 生出按鈕列、處理切換。util.js 固定最先載入,所以各模組執行時這個函式一定在。
//
// 這樣做是為了讓「加一個頁籤」只需要丟兩個新檔(web/partials/xxx.html +
// web/js/xxx.js),不必回頭改 index.html 或 main.js。順帶把「這一頁第一次開啟
// 要做什麼」寫回那一頁自己的檔裡 —— 舊版寫在 main.js 的 showTab,離實作很遠。

const TABS = [];

// tab: {
//   id           對應 panel-<id> 與 tab-<id>,也是 showTab(id) 的參數
//   label        按鈕上的文字
//   order        排序;現有四頁用 10/20/30/40,中間留空檔供日後插入
//   onInit()     頁面啟動時跑一次,不論這一頁是否顯示
//   onFirstShow() 第一次切到這一頁時
//   onShow()     每次切到這一頁時(含第一次,在 onFirstShow 之後)
// }
function registerTab(tab) {
  TABS.push(tab);
}

// 某一頁目前是否開著。取代散落各處的
// document.getElementById("panel-analysis").classList.contains("hidden") 反向判斷。
function isTabOpen(id) {
  const el = document.getElementById("panel-" + id);
  return !!el && !el.classList.contains("hidden");
}
