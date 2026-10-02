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

// ===== 自繪下拉選單 =====
// 原生 <select> 展開後的面板由作業系統畫 —— 方角、系統配色、自己的字體,
// 和頁面上收合時的樣子怎麼都對不起來,CSS 也管不到。這支把一個原生 select
// 包成「隱藏的 select + 自繪按鈕/面板」:
//
//   * select 本身留著(只是看不見),value / onchange / disabled 全照舊運作
//     → 既有程式碼(loadKeys 填 option、switchKey 讀 sel.value…)一行都不用改
//   * 面板是一般 DOM,吃得到 style.css,和頁面其他元件同一套語彙
//
// 用法:enhanceSelect(selectEl) 一次即可;之後 select 的 option 換了內容,
// 呼叫 el.refreshPicker() 或直接派 change 事件都會同步(見下方 MutationObserver)。
function enhanceSelect(sel) {
  if (!sel || sel.dataset.enhanced) return;
  sel.dataset.enhanced = "1";

  // 外層:select 的父層若已經是 .picker(如外觀選單,HTML 上先寫好了)就沿用,
  // 否則現做一個包住它。沿用的好處是那個外層可以另外掛自己的定位 class。
  let box = sel.parentElement;
  if (!box || !box.classList.contains("picker")) {
    box = document.createElement("div");
    box.className = "picker";
    sel.parentNode.insertBefore(box, sel);
    box.appendChild(sel);        // select 移進來(靠 CSS 藏起來),維持原本的表單語意
  }

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "picker-btn";
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  const lab = sel.getAttribute("aria-label");
  if (lab) btn.setAttribute("aria-label", lab);
  const menu = document.createElement("div");
  menu.className = "picker-menu";
  menu.setAttribute("role", "listbox");
  if (lab) menu.setAttribute("aria-label", lab);

  box.insertBefore(btn, sel);
  box.insertBefore(menu, sel);

  function close() {
    box.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", onOutside, true);
    document.removeEventListener("keydown", onKey);
  }
  function open() {
    if (sel.disabled) return;
    box.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
    document.addEventListener("click", onOutside, true);
    document.addEventListener("keydown", onKey);
  }
  function onOutside(e) { if (!box.contains(e.target)) close(); }
  function onKey(e) { if (e.key === "Escape") { close(); btn.focus(); } }

  // 依 select 目前的 option 重建面板。選項可能是動態產生的,
  // 所以每次內容變動都要重來一次。
  function rebuild() {
    menu.innerHTML = "";
    // 面板標題(可有可無)。外觀選單用它把「外觀」兩字收進選單裡。
    const head = sel.dataset.pickerHead;
    if (head) {
      const hd = document.createElement("div");
      hd.className = "picker-hd";
      hd.textContent = head;
      menu.appendChild(hd);
    }
    const cur = sel.value;
    for (const o of sel.options) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "picker-opt" + (o.value === cur ? " on" : "");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", o.value === cur ? "true" : "false");
      item.textContent = o.textContent;
      item.onclick = function () {
        sel.value = o.value;
        // 派發 change 讓原本掛在 select 上的 onchange(switchKey 等)照常觸發
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        rebuild();
        close();
      };
      menu.appendChild(item);
    }
    const on = sel.options[sel.selectedIndex];
    btn.textContent = on ? on.textContent : "";
    btn.disabled = sel.disabled;
    box.classList.toggle("disabled", sel.disabled);
  }

  btn.onclick = function () { box.classList.contains("open") ? close() : open(); };
  // 別處用程式改了 select(例如 buildGoalMonths 重填 option、或直接設 value)時同步。
  sel.addEventListener("change", rebuild);
  new MutationObserver(rebuild).observe(sel, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "value"],
  });
  sel.refreshPicker = rebuild;
  rebuild();
}

// 把頁面上所有標了 data-picker 的 select 都升級。partials 在 DOM 裡是現成的,
// 各頁的 js 之後才填 option —— 上面的 MutationObserver 會接住那些變動。
function enhanceAllSelects() {
  for (const s of document.querySelectorAll("select[data-picker]")) enhanceSelect(s);
}
