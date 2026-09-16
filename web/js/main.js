// ===== 主程式:分頁切換 + 啟動 =====
// 最後載入。此時 util 的註冊表與所有頁籤模組都已就緒。
//
// 這個檔不認得任何一個頁籤 —— 按鈕列、切換、per-tab 的時機全部由 util.js 的
// TABS 註冊表驅動。加一個頁籤只要丟 web/partials/xxx.html + web/js/xxx.js,
// 這裡一行都不用改。

const tabs = TABS.slice().sort((a, b) => (a.order || 0) - (b.order || 0));
const shownTabs = new Set();   // 已開過的頁籤 id,供 onFirstShow 判斷

function showTab(name) {
  for (const t of tabs) {
    const panel = document.getElementById("panel-" + t.id);
    const btn = document.getElementById("tab-" + t.id);
    if (panel) panel.classList.toggle("hidden", t.id !== name);
    if (btn) btn.classList.toggle("on", t.id === name);
  }
  const tab = tabs.find(t => t.id === name);
  if (!tab) return;
  if (!shownTabs.has(name)) {
    shownTabs.add(name);
    if (tab.onFirstShow) tab.onFirstShow();
  }
  if (tab.onShow) tab.onShow();
}

// ----- 頁籤按鈕列 -----
function buildTabs() {
  const bar = document.getElementById("tabs");
  if (!bar) return;
  bar.innerHTML = "";
  for (const t of tabs) {
    const btn = document.createElement("button");
    btn.className = "tab";
    btn.id = "tab-" + t.id;
    btn.textContent = t.label;
    btn.onclick = () => showTab(t.id);
    bar.appendChild(btn);
  }
}

// ----- 啟動 -----
buildTabs();
enhanceAllSelects();   // 原生下拉換成自繪版(見 util.js);各頁 js 稍後填的 option 會自動同步
for (const t of tabs) if (t.onInit) t.onInit();   // 各頁自己的初始化(不論是否顯示)
// 開第一頁。走 showTab 而不是直接設 class,第一頁才跟其他頁一樣拿得到
// onFirstShow / onShow —— 日後若有人用小一點的 order 插隊到最前面,
// 那一頁的初次載入不會因為「剛好排在第一個」就被跳過。
if (tabs.length) showTab(tabs[0].id);

loadConfig().then(loadKeys);   // 先讀設定,再讀驗證碼池(讓「目前驗證碼」那行勝出顯示)
loadPatents();
tickStatus(); tickJob();
setInterval(tickStatus, 5000);
setInterval(tickJob, 2000);
setInterval(updateJobElapsed, 1000);   // 執行中時讓「執行時間」每秒往上加
// 爬取統計不再每 5 秒盲刷:改由 tickStatus(每 5 秒輪詢一次,本來就為流量錶在跑)比對
// 伺服器的「資料變動簽章」,只有真的有新檢索紀錄(或跨時段/整點)時才刷新統計 → 見 onDataChanged。
