"""690 資轉監控:讀 s690 Rundeck 的通知信,判定各轉置系統的狀態,並在 Wiki.js 的登記表填格。

純邏輯(periods / parser / status / wikitable)不碰網路;對外連線只有 gmail_client 與 wiki。
service 負責組裝。流量監控(core)只在啟動時試著載入這個套件,載入失敗照常運作。
"""
