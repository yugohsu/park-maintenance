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
            if not needs_translation(text):
                continue
            if text not in cache:
                zh = translate(text)
                if zh:
                    cache[text] = zh
                    new_count += 1
                time.sleep(0.3)
            if text in cache:
                it["text_zh"] = cache[text]
                used[text] = cache[text]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    # 只保留目前還用得到的翻譯，檔案不會越來越大
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(used, f, ensure_ascii=False, indent=1)
    print(f"翻譯完成：新翻 {new_count} 句，共 {len(used)} 句有中文")


if __name__ == "__main__":
    main()
