// ===== 主程式:分頁切換 + 啟動 =====
// 最後載入。此時 util / monitor / control / analytics / progress 都已就緒,
// 可安全呼叫各模組的函式。

function showTab(name) {
  for (const t of ["monitor", "control", "analysis", "calc"]) {
    document.getElementById("panel-" + t).classList.toggle("hidden", t !== name);
    document.getElementById("tab-" + t).classList.toggle("on", t === name);
  }
  if (name === "analysis") tickAnalytics();
  // 檢索進度頁:首次開啟抓一次目前已存的資料,之後切分頁不再自動重算
  //(資料來自手動下載的匯出檔,沒換檔數字也不會變);要更新按「更新計算」。
  if (name === "calc" && !calcLoaded) { calcLoaded = true; refreshMonthly(); }
}

// ----- 啟動 -----
restoreDetect();        // 流量監控:可用上限偵測開關
restoreNotify();        // 流量監控:用量通知
restoreSurge();         // 流量監控:流量暴增預警
restoreExcludeLan();    // 流量監控:排除內網流量(稍後由 loadConfig 以伺服器為準覆寫)
restoreSpeedFloor();    // 流量監控:測速下限
restoreCalcLogCollapse(); // 流量監控:整合面板(網卡+用量明細)的收合/展開狀態(預設展開)
restoreMonitorTab();      // 流量監控:整合面板停在哪個頁籤(預設網卡)
updateFavicon(false);   // 先畫上正常分頁圖示(關掉提醒後會回到這個)
loadConfig().then(loadKeys);   // 先讀設定,再讀驗證碼池(讓「目前驗證碼」那行勝出顯示)
loadPatents();
tickStatus(); tickJob();
setInterval(tickStatus, 5000);
setInterval(tickJob, 2000);
setInterval(updateJobElapsed, 1000);   // 執行中時讓「執行時間」每秒往上加
// 爬取統計不再每 5 秒盲刷:改由 tickStatus(每 5 秒輪詢一次,本來就為流量錶在跑)比對
// 伺服器的「資料變動簽章」,只有真的有新檢索紀錄(或跨時段/整點)時才刷新統計 → 見 onDataChanged。
