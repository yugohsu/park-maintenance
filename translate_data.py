"""
把 docs/data.json 裡的日文／英文維修資訊翻成繁體中文（存成 text_zh 欄位）
翻過的句子會記在 docs/translations.json，下次不再重翻。
用法：python translate_data.py docs/data.json
"""
import json
import os
import re
import sys
import time
import urllib.parse

import requests

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "docs/data.json")), "translations.json")


# 設施名稱對照表（依導遊用的中文地圖）：日文／英文官方名稱 -> 地圖上的中文名稱
# 想新增或修改，照格式加一行即可
RIDE_NAMES = {
    # 長島溫泉樂園
    "スチールドラゴン2000": "鐵龍2000",
    "アクロバット": "特技飛行",
    "スターフライヤー": "星際飛行",
    "ジャイアントフリスビー": "超級飛盤",
    "スペースショット": "太空梭",
    "フライングカーペット": "飛天魔毯",
    "パラトルーパー": "五彩降落傘",
    "ジャンボバイキング": "巨無霸海盜船",
    "ジェットコースター": "雲霄飛車",
    "ワイルドマウス": "飛天滑鼠",
    "シュートザシュート": "激流湧進",
    "フリーフォール": "自由落體",
    "ジェットスキー": "摩托艇",
    "ロックンロール": "旋轉搖滾",
    "ウェーブスウィンガー": "旋轉鞦韆",
    "テレコンバット": "空中戰機",
    "大観覧車オーロラ": "極光大摩天輪",
    "ピーターラビットコースター": "彼得兔滑車",
    "マジックバイク": "魔術輕騎",
    "スピンボート": "旋轉船",
    "くるくるヘリコプター": "迴旋直升機",
    "ダックス": "臘腸狗",
    "ウォーターショット": "水槍戰士",
    "あしかさんのサーカス": "海獅先生的馬戲團",
    "チルドレンコースター": "小小飛車",
    "メリーゴーランド": "旋轉木馬",
    "バイキング": "海盜船",
    "ケロヨンジャンプ": "青蛙跳",
    "バルーンレース": "熱氣球",
    "そらとぶぞうさん": "飛天大象先生",
    "レッドバロン": "紅色戰士機",
    "モトクロス": "摩托車越野賽",
    "ティーカップ": "快樂轉杯",
    "サーキット2000": "環道2000",
    "ブランブラン": "盪盪鞦韆",
    "汽車": "蒸氣火車",
    "嵐": "暴風",
    # 樂高樂園
    "Lloyd's Spinjitzu Spinners": "Lloyd旋轉柔術旋轉器",
    "Anchors Away!": "拋錨",
    # 鈴鹿賽道樂園
    "コチラドライビングスクール": "酷奇拉駕駛學校",
    "テストフライト": "測試飛行員",
    "プッチパトロール": "小小巡邏隊",
    "ルンルンバルーン": "飄飄熱氣球",
    "チララのフラワーワゴン": "綺拉拉的花車",
    "でんでんむし": "點點蝸牛車",
    "ene-1（エネワン）": "節能競賽",
    "ロッキーコースター": "搖滾雲霄飛車",
    "モトファイター": "摩托戰士",
}

# 常見狀態用語
STATUS_WORDS = {
    "午後から運行予定": "預定下午開始運行",
    "運行予定": "預定開始運行",
    "一時運休": "暫時停駛",
    "終日運休": "全天停駛",
    "営業開始": "開始營業",
    "営業終了": "結束營業",
    "は休止します。": "暫停營運。",
    "For the time being": "暫停期間未定",
}


def apply_names(text: str) -> str:
    out = text
    for table in (RIDE_NAMES, STATUS_WORDS):
        for ja in sorted(table, key=len, reverse=True):
            if ja == "嵐" and out.strip() != "嵐":
                continue  # 「嵐」只在單獨出現時才換，避免誤換
            out = out.replace(ja, table[ja])
    return out


def needs_translation(text: str) -> bool:
    # 有日文假名，或是含英文單字（像 November、closed）
    return bool(re.search(r"[぀-ヿ]", text) or re.search(r"[A-Za-z]{3,}", text))


def google_translate(text: str) -> str:
    url = (
        "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-TW&dt=t&q="
        + urllib.parse.quote(text)
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()


def mymemory_translate(text: str) -> str:
    src = "ja" if re.search(r"[぀-ヿ]", text) else "en"
    r = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text[:480], "langpair": f"{src}|zh-TW"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["responseData"]["translatedText"].strip()


def translate(text: str) -> str:
    for fn in (google_translate, mymemory_translate):
        try:
            out = fn(text)
            if out:
                return out
        except Exception:  # noqa: BLE001
            continue
    return ""


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "docs/data.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}

    used, new_count = {}, 0
    for park in data.get("parks", []):
        for it in park.get("items", []):
            text = it["text"]
            base = apply_names(text)
            if not needs_translation(base):
                if base != text:
                    it["text_zh"] = base  # 對照表就翻完了，不用機器翻譯
                continue
            if base not in cache:
                zh = translate(base)
                if zh:
                    cache[base] = zh
                    new_count += 1
                time.sleep(0.3)
            if base in cache:
                it["text_zh"] = cache[base]
                used[base] = cache[base]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    # 只保留目前還用得到的翻譯，檔案不會越來越大
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(used, f, ensure_ascii=False, indent=1)
    print(f"翻譯完成：新翻 {new_count} 句，共 {len(used)} 句有中文")


if __name__ == "__main__":
    main()
