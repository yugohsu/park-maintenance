# 日本樂園設施維修查詢

查詢長島溫泉樂園、名古屋樂高樂園、鈴鹿賽道樂園、枚方公園在指定日期的設施維修／停止營運資訊。

## 手機網頁版
打開 GitHub Pages 網址，選日期、選樂園、按「查詢」。
資料由 GitHub Actions 每天自動抓兩次（台灣時間 05:17、17:17），存在 `docs/data.json`。
想立刻更新：到儲存庫的 **Actions → 每日更新樂園維修資料 → Run workflow**。

## 電腦桌面版（Windows）
```
pip install -r requirements.txt
python park_maintenance_checker.py
```
行程清單一行一筆：
```
2027-01-17 ~ 2027-01-22 長島溫泉樂園
2027-01-18 樂高樂園
```
清單會自動存檔，下次打開還在。

## 新增樂園
在 `park_maintenance_checker.py` 最上面的 `PARK_CONFIGS` 加一筆設定即可，網頁版會自動出現。
