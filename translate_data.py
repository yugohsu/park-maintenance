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
    "nagashima": {  # 長島溫泉樂園
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
    },
    "legoland": {  # 樂高樂園
        "Lloyd's Spinjitzu Spinners": "Lloyd旋轉柔術旋轉器",
        "Anchors Away!": "拋錨",
    },
    "suzuka": {  # 鈴鹿賽道樂園
        "コチラドライビングスクール": "酷奇拉駕駛學校",
        "テストフライト": "測試飛行員",
        "プッチパトロール": "小小巡邏隊",
        "ルンルンバルーン": "飄飄熱氣球",
        "チララのフラワーワゴン": "綺拉拉的花車",
        "でんでんむし": "點點蝸牛車",
        "ene-1（エネワン）": "節能競賽",
        "ロッキーコースター": "搖滾雲霄飛車",
        "モトファイター": "摩托戰士",
    },
    "hirakata": {  # 枚方公園
        "ヘアピンコースター クレージーマウス": "急轉過山車 瘋狂老鼠",
        "クレージーマウス": "急轉過山車 瘋狂老鼠",
        "レッドファルコン": "紅色獵鷹",
        "ドルフィンパラダイス": "海豚樂園",
        "木製コースター エルフ": "木製過山車 ELF",
        "エルフ": "木製過山車 ELF",
        "トロールパニック パチャンガ": "激流勇進 PACHANGA",
        "パチャンガ": "激流勇進 PACHANGA",
        "スカイウォーカー": "摩天輪 天空行者",
        "ウィザードのまじかるジャンピン": "法師的魔幻跳躍",
        "カイトフライヤー": "風箏飛人",
        "サーキット2000": "環形飛車2000",
        "メリーゴーランド": "旋轉木馬",
    },
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


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
WEEKDAYS = {"月": "週一", "火": "週二", "水": "週三", "木": "週四", "金": "週五", "土": "週六", "日": "週日", "祝": "假日"}


def _en_date(m):
    mo = MONTHS.get(m.group(1).lower())
    return f"{m.group(3)}/{mo}/{m.group(2)}" if mo else m.group(0)


def apply_names(text: str, park_id: str = "") -> str:
    out = text
    for table in (RIDE_NAMES.get(park_id, {}), STATUS_WORDS):
        for ja in sorted(table, key=len, reverse=True):
            if ja == "嵐" and out.strip() != "嵐":
                continue  # 「嵐」只在單獨出現時才換，避免誤換
            out = out.replace(ja, table[ja])
    # 英文日期 November 30, 2026 -> 2026/11/30
    out = re.sub(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", _en_date, out)
    # 日文星期 (火) -> (週二)
    out = re.sub(r"[（(]([月火水木金土日祝])[）)]", lambda m: f"({WEEKDAYS[m.group(1)]})", out)
    return out


# 還沒翻到的日文／英文片段（例如對照表沒有的設施名稱）
LEFTOVER_RE = re.compile(r"[\u3040-\u30ffー・][\u3040-\u30ffー・A-Za-z0-9]*|[A-Za-z][A-Za-z'&!\-]*(?:\s+[A-Za-z][A-Za-z'&!\-]*)*")


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
            base = apply_names(text, park.get("id", ""))
            zh = base
            # 只把剩下的日文／英文片段送去機器翻譯，其餘文字保持不動
            for seg in sorted(set(m.group(0).strip() for m in LEFTOVER_RE.finditer(base)), key=len, reverse=True):
                if not seg or not needs_translation(seg):
                    continue
                if any(seg in v for t in RIDE_NAMES.values() for v in t.values()):
                    continue  # 地圖中文名稱裡本來就有的英文（例如 Lloyd），不翻
                if seg not in cache:
                    t = translate(seg)
                    if t:
                        cache[seg] = t
                        new_count += 1
                    time.sleep(0.3)
                if seg in cache:
                    used[seg] = cache[seg]
                    zh = zh.replace(seg, cache[seg])
            if zh != text:
                it["text_zh"] = zh

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    # 只保留目前還用得到的翻譯，檔案不會越來越大
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(used, f, ensure_ascii=False, indent=1)
    print(f"翻譯完成：新翻 {new_count} 句，共 {len(used)} 句有中文")


if __name__ == "__main__":
    main()
