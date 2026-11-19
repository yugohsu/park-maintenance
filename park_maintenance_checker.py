"""
日本樂園設施維修查詢工具（優化版）

兩種用法：
1. 桌面版（Windows 雙擊或 python park_maintenance_checker.py）
   -> 開啟 USJ 藍黃配色的查詢視窗
2. 匯出資料（給 GitHub Actions / 手機網頁版使用）
   -> python park_maintenance_checker.py --export docs/data.json --days 240
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# 1. 樂園設定區（未來要新增樂園，直接在此加入設定即可）
# ==============================================================================
PARK_CONFIGS = [
    {
        "id": "nagashima",
        "name": "長島溫泉樂園",
        "aliases": ["長島", "長島樂園", "長島溫泉", "Nagashima", "ナガシマ"],
        "url": "https://www.nagashima-onsen.co.jp/spaland/attraction/status.html#attraction01",
    },
    {
        "id": "legoland",
        "name": "樂高樂園",
        "aliases": ["樂高", "名古屋樂高", "LEGOLAND", "レゴランド"],
        "url": "https://www.legoland.jp/zh-hant/operation/info/closed/",
    },
    {
        "id": "suzuka",
        "name": "鈴鹿賽道樂園",
        "aliases": ["鈴鹿", "鈴鹿賽道", "Suzuka"],
        "url": "https://www.suzukacircuit.jp/park/schedule/",
    },
    {
        "id": "hirakata",
        "name": "枚方公園",
        "aliases": ["枚方", "Hirakata", "ひらかたパーク"],
        "url": "https://www.hirakatapark.co.jp/situation/",
        "selectors": ["tr", "li", "dd", "p"],
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7",
}

MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

INDEFINITE_KEYWORDS = [
    "当面の間", "当面の期間", "しばらくの間", "未定", "for the time being",
    "until further notice", "另行通知", "另行公告", "暫定", "期限未定",
]

# 日文 + 繁中 + 英文 維修／停止關鍵字
MAINTENANCE_KEYWORDS = [
    "運休", "休止", "メンテナンス", "点検", "整備", "クローズ", "休業", "休園",
    "運転見合わせ", "営業終了", "改装", "工事", "利用不可", "中止",
    "停駛", "暫停", "停止", "維修", "保養", "檢修", "關閉", "施工", "休館",
    "closed", "maintenance", "suspended",
]

FETCH_TIMEOUT = 20
CACHE_TTL_SECONDS = 600  # 同一網址 10 分鐘內不重抓


# ==============================================================================
# 2. 網路連線（自動重試 + 快取）
# ==============================================================================
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=3, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


_SESSION = _build_session()
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def fetch_text(url: str, encoding: str = None) -> str:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    resp = _SESSION.get(url, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    if encoding:
        resp.encoding = encoding
    elif not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    text = resp.text
    with _CACHE_LOCK:
        _CACHE[url] = (now, text)
    return text


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


# ==============================================================================
# 3. 共用日期工具
# ==============================================================================
def safe_date(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def ranges_overlap(s1, e1, s2, e2) -> bool:
    """e 為 None 代表「無結束日（長期休止）」"""
    if e1 is None:
        e1 = date.max
    if e2 is None:
        e2 = date.max
    return s1 <= e2 and e1 >= s2


def infer_year(m: int, d: int, win_start: date, win_end: date):
    """沒寫年份的日期：挑最接近查詢期間的年份（可正確處理跨年行程）"""
    best, best_dist = None, None
    for y in range(win_start.year - 1, win_end.year + 2):
        dt = safe_date(y, m, d)
        if not dt:
            continue
        if win_start <= dt <= win_end:
            dist = 0
        else:
            dist = min(abs((dt - win_start).days), abs((dt - win_end).days))
        if best_dist is None or dist < best_dist:
            best, best_dist = dt, dist
    return best


def _parse_en_date(s: str):
    m = re.match(r"\s*([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})", s)
    if m and MONTH_MAP.get(m.group(1).lower()):
        return safe_date(m.group(3), MONTH_MAP[m.group(1).lower()], m.group(2))
    m = re.match(r"\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s*(\d{4})", s)
    if m and MONTH_MAP.get(m.group(2).lower()):
        return safe_date(m.group(3), MONTH_MAP[m.group(2).lower()], m.group(1))
    return None


_EN_DATE = r"(?:[A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\.?,?\s*\d{4})"
_WEEKDAY = r"(?:\s*[（(][^（）()]{1,6}[）)])?"
_SEP = r"\s*(?:～|〜|~|－|-|–|—|から|至|到|to)\s*"

EN_RANGE_RE = re.compile(rf"({_EN_DATE}){_WEEKDAY}{_SEP}({_EN_DATE})", re.IGNORECASE)
EN_SINGLE_RE = re.compile(_EN_DATE, re.IGNORECASE)

# 數字／中日文日期，例如 2026年11月30日、2026/11/30、2026-11-30、11月30日、11/30
CJK_RANGE_RE = re.compile(
    r"(?<!\d)(?:(\d{4})\s*[年/\.\-]\s*)?(\d{1,2})\s*[月/\.\-]\s*(\d{1,2})(?!\d)\s*日?"
    + _WEEKDAY + _SEP +
    r"(?:(\d{4})\s*[年/\.\-]\s*)?(?:(\d{1,2})\s*[月/\.\-]\s*)?(\d{1,2})(?!\d)\s*日?"
)
CJK_SINGLE_RE = re.compile(
    r"(?<!\d)(?:(\d{4})\s*[年/\.\-]\s*)?(\d{1,2})\s*[月/\.\-]\s*(\d{1,2})(?!\d)\s*日?"
)


def extract_periods(text: str, win_start: date, win_end: date) -> list:
    """
    從一段文字找出所有日期期間，回傳 [(開始日, 結束日或 None), ...]
    結束日為 None 表示「當面の間／另行通知」等長期休止。
    """
    periods = []
    used = []  # 已被期間吃掉的字元範圍，避免重複當成單日

    def overlaps_used(a, b):
        return any(a < ub and b > ua for ua, ub in used)

    # (1) 英文日期區間（全部找出，不只第一個）
    for m in EN_RANGE_RE.finditer(text):
        d1, d2 = _parse_en_date(m.group(1)), _parse_en_date(m.group(2))
        if d1 and d2:
            if d2 < d1:
                d1, d2 = d2, d1
            periods.append((d1, d2))
            used.append(m.span())

    # (2) 數字／中日文日期區間
    for m in CJK_RANGE_RE.finditer(text):
        if overlaps_used(*m.span()):
            continue
        sy, sm, sd, ey, em, ed = m.groups()
        sm, sd, ed = int(sm), int(sd), int(ed)
        if sy:
            d1 = safe_date(sy, sm, sd)
        else:
            d1 = infer_year(sm, sd, win_start, win_end) if safe_date(2024, sm, sd) else None
        if not d1:
            continue
        em_i = int(em) if em else sm
        d2 = safe_date(ey or d1.year, em_i, ed)
        if d2 and d2 < d1 and not ey:
            # 跨月或跨年：11/28～3 視為 12/3；12/28～1/5 視為隔年 1/5
            if not em:
                nm = sm + 1 if sm < 12 else 1
                d2 = safe_date(d1.year + (1 if sm == 12 else 0), nm, ed)
            else:
                d2 = safe_date(d1.year + 1, em_i, ed)
        if d2 and d2 >= d1:
            periods.append((d1, d2))
            used.append(m.span())

    lower = text.lower()
    indefinite = any(k.lower() in lower for k in INDEFINITE_KEYWORDS)

    # (3) 單一日期（不在上面期間內的）
    singles = []
    for m in EN_SINGLE_RE.finditer(text):
        if overlaps_used(*m.span()):
            continue
        d = _parse_en_date(m.group(0))
        if d:
            singles.append(d)
            used.append(m.span())
    for m in CJK_SINGLE_RE.finditer(text):
        if overlaps_used(*m.span()):
            continue
        y, mo, d = m.groups()
        mo, d = int(mo), int(d)
        if not safe_date(2024, mo, d):
            continue
        dt = safe_date(y, mo, d) if y else infer_year(mo, d, win_start, win_end)
        if dt:
            singles.append(dt)

    if indefinite and singles:
        # 「2025/5/16～当面の間」：最早日期起，無結束日
        periods.append((min(singles), None))
    else:
        periods.extend((d, d) for d in singles)

    # 去除重複並排序
    uniq = sorted(set(periods), key=lambda p: (p[0], p[1] or date.max))
    return uniq


def format_period(p) -> str:
    s, e = p
    if e is None:
        return f"{s} 起（長期休止）"
    if s == e:
        return str(s)
    return f"{s} ~ {e}"


def has_keyword(text: str) -> bool:
    lower = text.lower()
    return any(k.lower() in lower for k in MAINTENANCE_KEYWORDS)


def clean_text(s: str) -> str:
    return " ".join(s.split())


def dedupe_containers(items: list) -> list:
    """網頁巢狀標籤會重複抓到同一段文字（外層包含內層），保留外層（含設施名稱＋日期）"""
    texts = [it["text"] for it in items]
    keep = []
    for i, it in enumerate(items):
        t = it["text"]
        if any(j != i and texts[j] != t and t in texts[j] for j in range(len(items))):
            continue
        if any(k["text"] == t for k in keep):
            continue
        keep.append(it)
    return keep


# ==============================================================================
# 4. 各樂園解析器
#    load()  ：抓原始資料（有快取）
#    items() ：解析成 [{"text":..., "periods":[(s,e),...]}]
#    check() ：依查詢期間篩選
# ==============================================================================
class BaseParkChecker:
    require_keyword = True
    skip_re = None  # 沒有關鍵字時，符合這個規則的文字會被略過（例如營業時間）
    min_len, max_len = 4, 250

    def __init__(self, config: dict):
        self.id = config["id"]
        self.name = config["name"]
        self.aliases = config.get("aliases", [])
        self.url = config["url"]
        self.selectors = config.get("selectors", ["tr", "li", "p"])

    # ---- 抓資料 ----
    def load(self):
        return fetch_text(self.url.split("#")[0])

    # ---- 解析 ----
    def _soup(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        return soup

    def _elements(self, soup):
        return soup.select(", ".join(self.selectors))

    def items(self, raw, win_start: date, win_end: date) -> list:
        soup = self._soup(raw)
        found = []
        for el in self._elements(soup):
            text = clean_text(el.get_text(" ", strip=True))
            if not (self.min_len <= len(text) <= self.max_len):
                continue
            kw = has_keyword(text)
            if self.require_keyword and not kw:
                continue
            if not kw and self.skip_re and self.skip_re.search(text):
                continue
            periods = extract_periods(text, win_start, win_end)
            if periods:
                found.append({"text": text, "periods": periods})
        return dedupe_containers(found)

    # ---- 查詢 ----
    def check(self, q_start: date, q_end: date) -> dict:
        base = {
            "park": self.name,
            "id": self.id,
            "range": f"{q_start} ~ {q_end}" if q_start != q_end else str(q_start),
            "url": self.url,
            "items": [],
        }
        try:
            raw = self.load()
            matched = []
            for it in self.items(raw, q_start, q_end):
                hit = [p for p in it["periods"] if ranges_overlap(p[0], p[1], q_start, q_end)]
                if hit:
                    matched.append({"text": it["text"], "periods": hit})
            matched.sort(key=lambda x: (x["periods"][0][0], x["text"]))
            base.update(status="ok", items=matched)
        except Exception as e:  # noqa: BLE001
            base.update(status="error", error=friendly_error(e))
        return base


class NagashimaChecker(BaseParkChecker):
    """長島溫泉樂園：解析「定期運休情報」的月份表格；表格抓不到時改用通用解析"""

    def items(self, raw, win_start, win_end):
        soup = BeautifulSoup(raw, "html.parser")
        found = {}
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            # 月份可能在表格標題（caption）或第一列，也可能在表格前面的標題
            header_candidates = []
            cap = table.find("caption")
            if cap:
                header_candidates.append(cap.get_text(" ", strip=True))
            header_candidates.append(rows[0].get_text(" ", strip=True))
            prev = table.find_previous(["h2", "h3", "h4", "h5", "p", "dt"])
            if prev:
                header_candidates.append(prev.get_text(" ", strip=True))

            table_year, table_month = None, None
            for h in header_candidates:
                m = re.search(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月", h)
                if m and 1 <= int(m.group(2)) <= 12:
                    table_year = int(m.group(1)) if m.group(1) else None
                    table_month = int(m.group(2))
                    break
            if not table_month:
                continue

            for tr in rows:
                cols = [clean_text(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                if len(cols) < 2:
                    continue
                m_day = re.search(r"(\d{1,2})\s*日", cols[0])
                if not m_day:
                    continue
                day_num = int(m_day.group(1))
                ride_info = " ".join(c for c in cols[1:] if c).strip()
                if not ride_info or ride_info in ("-", "－", "―", "なし", "ー"):
                    continue
                if table_year:
                    d = safe_date(table_year, table_month, day_num)
                else:
                    d = infer_year(table_month, day_num, win_start, win_end)
                if not d:
                    continue
                found.setdefault(ride_info, set()).add((d, d))

        result = [{"text": t, "periods": sorted(ps)} for t, ps in found.items()]
        if result:
            return result
        return super().items(raw, win_start, win_end)


class LegolandChecker(BaseParkChecker):
    """樂高樂園：此頁本身就是「停止營運公告」，所以不強制需要關鍵字"""
    require_keyword = False
    skip_re = re.compile(r"營業時間|開園|營業日|票價|\d{1,2}:\d{2}")
    min_len, max_len = 8, 300

    def _elements(self, soup):
        els = soup.select("main li, main tr, main p, main dd, article, .accordion, section div")
        return [el for el in els if len(el.find_all(["div", "section"])) <= 2]


class SuzukaChecker(BaseParkChecker):
    """鈴鹿賽道樂園：擷取網頁內嵌的 Google 日曆，直接解析 .ics"""

    def _extract_calendar_ids(self, html: str) -> list:
        cal_ids = []
        urls = re.findall(r"https?://calendar\.google\.com/calendar/[^\s\"'<>]+", html)
        for u in urls:
            u = u.replace("&amp;", "&")
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            for src in qs.get("src", []) + qs.get("cid", []):
                if src not in cal_ids:
                    cal_ids.append(src)
        return cal_ids

    def load(self):
        html = fetch_text(self.url)
        cal_ids = self._extract_calendar_ids(html)
        if not cal_ids:
            raise RuntimeError("網頁中找不到 Google 日曆，請點「開啟官網」自行確認。")
        ics_list = []
        for cid in cal_ids:
            ics_url = f"https://calendar.google.com/calendar/ical/{urllib.parse.quote(cid)}/public/basic.ics"
            try:
                ics_list.append(fetch_text(ics_url, encoding="utf-8"))
            except Exception:  # noqa: BLE001  單一日曆失敗不影響其他日曆
                continue
        if not ics_list:
            raise RuntimeError("Google 日曆讀取失敗，請點「開啟官網」自行確認。")
        return ics_list

    def items(self, raw, win_start, win_end):
        found = {}
        for ics_text in raw:
            unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
            for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.DOTALL):
                m_sum = re.search(r"^SUMMARY(?:;[^:\r\n]*)?:(.*)$", ev, re.MULTILINE)
                m_start = re.search(r"^DTSTART(?:;([^:\r\n]*))?:(\d{8})", ev, re.MULTILINE)
                m_end = re.search(r"^DTEND(?:;([^:\r\n]*))?:(\d{8})", ev, re.MULTILINE)
                if not (m_sum and m_start):
                    continue
                summary = (
                    m_sum.group(1).strip()
                    .replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\N", " ")
                )
                s = m_start.group(2)
                ev_start = safe_date(s[:4], s[4:6], s[6:8])
                if not ev_start:
                    continue
                ev_end = ev_start
                if m_end:
                    e = m_end.group(2)
                    end_d = safe_date(e[:4], e[4:6], e[6:8])
                    # 全天事件的 DTEND 是「隔天」，要減一天
                    if end_d and m_end.group(1) and "VALUE=DATE" in m_end.group(1).upper():
                        end_d -= timedelta(days=1)
                    if end_d and end_d >= ev_start:
                        ev_end = end_d
                found.setdefault(clean_text(summary), set()).add((ev_start, ev_end))
        return [{"text": t, "periods": sorted(ps)} for t, ps in found.items()]


CUSTOM_CHECKER_CLASSES = {
    "nagashima": NagashimaChecker,
    "legoland": LegolandChecker,
    "suzuka": SuzukaChecker,
}

PARK_REGISTRY = [CUSTOM_CHECKER_CLASSES.get(c["id"], BaseParkChecker)(c) for c in PARK_CONFIGS]


def friendly_error(e: Exception) -> str:
    if isinstance(e, requests.exceptions.Timeout):
        return "官網回應太慢（逾時），請稍後再試或點「開啟官網」確認。"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "連不上官網，請檢查網路後再試。"
    if isinstance(e, requests.exceptions.HTTPError):
        code = e.response.status_code if e.response is not None else "?"
        return f"官網拒絕連線（代碼 {code}），請點「開啟官網」確認。"
    return str(e)


# ==============================================================================
# 5. 行程文字解析
# ==============================================================================
def find_park_checker(keyword: str):
    kw = keyword.strip().lower()
    if not kw:  # 原版空字串會誤配到第一個樂園
        return None
    for checker in PARK_REGISTRY:
        names = [checker.name] + checker.aliases
        for n in names:
            n = n.lower()
            if n in kw or (len(kw) >= 2 and kw in n):
                return checker
    return None


_LINE_DATE_RE = re.compile(r"(\d{4})\s*[-/\.年]\s*(\d{1,2})\s*[-/\.月]\s*(\d{1,2})\s*日?")


def parse_itinerary_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None, None
    found = list(_LINE_DATE_RE.finditer(line))
    if not found:
        return None, None, None
    d1 = safe_date(*found[0].groups())
    d2 = safe_date(*found[1].groups()) if len(found) >= 2 else d1
    if not d1 or not d2:
        return None, None, None
    if d2 < d1:
        d1, d2 = d2, d1
    rest = _LINE_DATE_RE.sub(" ", line)
    rest = re.sub(r"[~～〜\-－–—至到]", " ", rest)
    checker = find_park_checker(rest)
    return d1, d2, checker


def run_checks(tasks, max_workers: int = 4) -> list:
    """tasks: [(d1, d2, checker), ...]；同時查詢多個樂園，同一網址只抓一次"""
    unique = {c.id: c for _, _, c in tasks}

    def preload(c):
        try:
            c.load()
        except Exception:  # noqa: BLE001  錯誤會在 check() 時再回報
            pass

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(preload, unique.values()))
        return list(ex.map(lambda t: t[2].check(t[0], t[1]), tasks))


def results_to_text(results: list, skipped: list = None) -> str:
    out = []
    for res in results:
        out.append("=" * 40)
        out.append(f"【{res['range']}】{res['park']}")
        if res["status"] == "error":
            out.append(f"查詢失敗：{res['error']}")
        elif res["items"]:
            out.append(f"維修／停止設施（共 {len(res['items'])} 筆）：")
            for it in res["items"]:
                label = "、".join(format_period(p) for p in it["periods"])
                out.append(f"  ・[{label}] {it['text']}")
        else:
            out.append("維修設施：無")
        out.append(f"官網：{res['url']}")
        out.append("")
    if skipped:
        out.append("-" * 40)
        out.append("無法辨識的行程（已略過）：")
        out.extend(f"  ・{s}" for s in skipped)
    return "\n".join(out)


# ==============================================================================
# 6. 匯出 JSON（GitHub Actions 每天自動執行，給手機網頁版讀取）
# ==============================================================================
def export_json(path: str, days: int = 240, past_days: int = 7):
    today = datetime.now(timezone(timedelta(hours=9))).date()  # 日本時間
    win_start, win_end = today - timedelta(days=past_days), today + timedelta(days=days)
    parks = []

    def work(c):
        entry = {"id": c.id, "name": c.name, "url": c.url, "items": []}
        try:
            raw = c.load()
            for it in c.items(raw, win_start, win_end):
                ps = [p for p in it["periods"] if ranges_overlap(p[0], p[1], win_start, win_end)]
                if ps:
                    entry["items"].append({
                        "text": it["text"],
                        "periods": [[str(s), str(e) if e else None] for s, e in ps],
                    })
            entry["items"].sort(key=lambda x: (x["periods"][0][0], x["text"]))
            entry["status"] = "ok"
        except Exception as e:  # noqa: BLE001
            entry["status"] = "error"
            entry["error"] = friendly_error(e)
        return entry

    with ThreadPoolExecutor(max_workers=4) as ex:
        parks = list(ex.map(work, PARK_REGISTRY))

    data = {
        "generated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "window": [str(win_start), str(win_end)],
        "parks": parks,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for p in parks:
        status = f"{len(p['items'])} 筆" if p["status"] == "ok" else f"失敗：{p.get('error')}"
        print(f"{p['name']}: {status}")
    print(f"已輸出 {path}")


# ==============================================================================
# 7. 桌面版圖形介面（USJ 藍黃配色、大字）
# ==============================================================================
USJ_NAVY = "#0A2A66"
USJ_BLUE = "#1450B4"
USJ_SKY = "#E8F0FC"
USJ_YELLOW = "#FFC72C"
USJ_YELLOW_DARK = "#E6A800"
WHITE = "#FFFFFF"
RED = "#C62828"
FONT = "Microsoft JhengHei"
SAVE_FILE = os.path.join(os.path.expanduser("~"), ".park_checker_itinerary.txt")


def run_gui():
    import tkinter as tk
    import webbrowser
    from tkinter import ttk, scrolledtext, messagebox

    class MaintenanceApp:
        def __init__(self, root):
            self.root = root
            root.title("日本樂園設施維修查詢")
            root.geometry("980x780")
            root.minsize(760, 600)
            root.configure(bg=USJ_SKY)
            self.last_urls = {}
            self._style()
            self._build_ui()
            root.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---------- 外觀 ----------
        def _style(self):
            st = ttk.Style()
            try:
                st.theme_use("clam")
            except tk.TclError:
                pass
            st.configure("TCombobox", padding=6, font=(FONT, 13, "bold"))
            self.root.option_add("*TCombobox*Listbox.font", (FONT, 13, "bold"))

        def _button(self, parent, text, cmd, big=False, primary=True):
            bg = USJ_YELLOW if primary else USJ_BLUE
            fg = USJ_NAVY if primary else WHITE
            active = USJ_YELLOW_DARK if primary else USJ_NAVY
            return tk.Button(
                parent, text=text, command=cmd, bg=bg, fg=fg,
                activebackground=active, activeforeground=fg if primary else WHITE,
                font=(FONT, 18 if big else 13, "bold"), relief="flat", bd=0,
                padx=24 if big else 14, pady=10 if big else 6, cursor="hand2",
            )

        def _section(self, title):
            frame = tk.Frame(self.root, bg=WHITE, highlightbackground=USJ_BLUE, highlightthickness=2)
            frame.pack(fill="x", padx=16, pady=(10, 0))
            tk.Label(frame, text=title, bg=USJ_BLUE, fg=WHITE, font=(FONT, 13, "bold"),
                     anchor="w", padx=10, pady=4).pack(fill="x")
            body = tk.Frame(frame, bg=WHITE)
            body.pack(fill="x", padx=10, pady=8)
            return body

        # ---------- 版面 ----------
        def _build_ui(self):
            header = tk.Frame(self.root, bg=USJ_NAVY)
            header.pack(fill="x")
            tk.Label(header, text="日本樂園設施維修查詢", bg=USJ_NAVY, fg=USJ_YELLOW,
                     font=(FONT, 22, "bold"), pady=10).pack(side="left", padx=16)
            tk.Label(header, text="支援：" + "、".join(p.name for p in PARK_REGISTRY),
                     bg=USJ_NAVY, fg=WHITE, font=(FONT, 11, "bold")).pack(side="right", padx=16)

            # 快速加入
            q = self._section("快速加入行程")
            today = date.today()
            lbl = dict(bg=WHITE, fg=USJ_NAVY, font=(FONT, 13, "bold"))
            ent = dict(font=("Consolas", 14, "bold"), width=11, relief="solid", bd=1)
            tk.Label(q, text="開始", **lbl).grid(row=0, column=0, padx=(0, 4))
            self.start_entry = tk.Entry(q, **ent)
            self.start_entry.insert(0, str(today))
            self.start_entry.grid(row=0, column=1, padx=4, ipady=4)
            tk.Label(q, text="結束", **lbl).grid(row=0, column=2, padx=(12, 4))
            self.end_entry = tk.Entry(q, **ent)
            self.end_entry.insert(0, str(today + timedelta(days=5)))
            self.end_entry.grid(row=0, column=3, padx=4, ipady=4)
            tk.Label(q, text="樂園", **lbl).grid(row=0, column=4, padx=(12, 4))
            self.park_combo = ttk.Combobox(q, values=[p.name for p in PARK_REGISTRY],
                                           state="readonly", width=12, font=(FONT, 13, "bold"))
            self.park_combo.current(0)
            self.park_combo.grid(row=0, column=5, padx=4)
            self._button(q, "加入", self.add_to_itinerary, primary=False).grid(row=0, column=6, padx=(12, 0))

            # 行程清單
            it = self._section("行程清單（一行一筆：2027-01-17 ~ 2027-01-22 長島溫泉樂園）")
            self.itinerary_text = scrolledtext.ScrolledText(
                it, height=5, font=("Consolas", 13, "bold"), relief="flat",
                bg=USJ_SKY, fg=USJ_NAVY, insertbackground=USJ_NAVY)
            self.itinerary_text.pack(fill="x")
            self.itinerary_text.insert("1.0", self._load_itinerary())

            # 按鈕列
            bar = tk.Frame(self.root, bg=USJ_SKY)
            bar.pack(fill="x", padx=16, pady=12)
            self.search_btn = self._button(bar, "開始查詢", self.start_search, big=True)
            self.search_btn.pack(side="left")
            self._button(bar, "複製結果", self.copy_result, primary=False).pack(side="left", padx=(12, 0))
            self._button(bar, "開啟官網", self.open_all_urls, primary=False).pack(side="left", padx=(8, 0))
            self._button(bar, "清空清單", self.clear_itinerary, primary=False).pack(side="left", padx=(8, 0))
            self.status_var = tk.StringVar(value="就緒")
            tk.Label(bar, textvariable=self.status_var, bg=USJ_SKY, fg=USJ_NAVY,
                     font=(FONT, 13, "bold")).pack(side="right")

            # 結果
            res = tk.Frame(self.root, bg=WHITE, highlightbackground=USJ_BLUE, highlightthickness=2)
            res.pack(fill="both", expand=True, padx=16, pady=(0, 16))
            tk.Label(res, text="查詢結果", bg=USJ_BLUE, fg=WHITE, font=(FONT, 13, "bold"),
                     anchor="w", padx=10, pady=4).pack(fill="x")
            self.result_text = scrolledtext.ScrolledText(
                res, font=(FONT, 13, "bold"), wrap="word", relief="flat",
                bg=WHITE, fg="#1A1A1A", padx=10, pady=8)
            self.result_text.pack(fill="both", expand=True)
            rt = self.result_text
            rt.tag_configure("park", font=(FONT, 16, "bold"), foreground=WHITE,
                             background=USJ_NAVY, spacing1=8, spacing3=4, lmargin1=6)
            rt.tag_configure("date", background=USJ_YELLOW, foreground=USJ_NAVY)
            rt.tag_configure("none", foreground=USJ_BLUE)
            rt.tag_configure("error", foreground=RED)
            rt.tag_configure("url", foreground=USJ_BLUE, underline=True)
            rt.tag_bind("url", "<Enter>", lambda e: rt.config(cursor="hand2"))
            rt.tag_bind("url", "<Leave>", lambda e: rt.config(cursor=""))

        # ---------- 行程清單 ----------
        def _load_itinerary(self):
            try:
                with open(SAVE_FILE, encoding="utf-8") as f:
                    txt = f.read().strip()
                    if txt:
                        return txt + "\n"
            except OSError:
                pass
            t, e = date.today(), date.today() + timedelta(days=5)
            return "".join(f"{t} ~ {e} {p.name}\n" for p in PARK_REGISTRY[:3])

        def _save_itinerary(self):
            try:
                with open(SAVE_FILE, "w", encoding="utf-8") as f:
                    f.write(self.itinerary_text.get("1.0", "end").strip())
            except OSError:
                pass

        def on_close(self):
            self._save_itinerary()
            self.root.destroy()

        def add_to_itinerary(self):
            s_str, e_str = self.start_entry.get().strip(), self.end_entry.get().strip()
            d1 = parse_itinerary_line(s_str + " x")[0] if s_str else None
            d2 = parse_itinerary_line(e_str + " x")[0] if e_str else d1
            if not d1 or not d2:
                messagebox.showwarning("日期格式", "日期請用 2027-01-17 這種格式。")
                return
            if d2 < d1:
                d1, d2 = d2, d1
            p = self.park_combo.get()
            line = f"{d1} ~ {d2} {p}" if d1 != d2 else f"{d1} {p}"
            cur = self.itinerary_text.get("1.0", "end")
            if not cur.endswith("\n\n") and cur.strip():
                self.itinerary_text.insert("end-1c", "" if cur.endswith("\n") else "\n")
            self.itinerary_text.insert("end-1c", line + "\n")
            self._save_itinerary()

        def clear_itinerary(self):
            if messagebox.askyesno("清空清單", "確定要清空行程清單嗎？"):
                self.itinerary_text.delete("1.0", "end")
                self._save_itinerary()

        # ---------- 查詢 ----------
        def start_search(self):
            tasks, skipped = [], []
            for line in self.itinerary_text.get("1.0", "end").splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                d1, d2, checker = parse_itinerary_line(line)
                if d1 and d2 and checker:
                    tasks.append((d1, d2, checker))
                else:
                    skipped.append(line.strip())
            if not tasks:
                messagebox.showwarning(
                    "格式提示",
                    "找不到可辨識的行程，請用以下格式：\n"
                    "期間：2027-01-17 ~ 2027-01-22 長島溫泉樂園\n"
                    "單日：2027-01-18 樂高樂園")
                return
            self._save_itinerary()
            self.search_btn.config(state="disabled", text="查詢中…")
            self.result_text.delete("1.0", "end")
            self.last_urls.clear()
            self.status_var.set(f"查詢 {len(tasks)} 筆行程中…")
            threading.Thread(target=self._worker, args=(tasks, skipped), daemon=True).start()

        def _worker(self, tasks, skipped):
            clear_cache()  # 每次按查詢都抓最新資料
            try:
                results = run_checks(tasks)
            except Exception as e:  # noqa: BLE001
                results = []
                self.root.after(0, messagebox.showerror, "錯誤", str(e))
            self.root.after(0, self._show, results, skipped)

        def _show(self, results, skipped):
            rt = self.result_text
            total = 0
            for res in results:
                self.last_urls[res["park"]] = res["url"]
                rt.insert("end", f" {res['range']}  {res['park']} \n", "park")
                if res["status"] == "error":
                    rt.insert("end", f"查詢失敗：{res['error']}\n", "error")
                elif res["items"]:
                    total += len(res["items"])
                    for it in res["items"]:
                        label = "、".join(format_period(p) for p in it["periods"])
                        rt.insert("end", "・")
                        rt.insert("end", f" {label} ", "date")
                        rt.insert("end", f" {it['text']}\n")
                else:
                    rt.insert("end", "這段期間沒有找到維修設施\n", "none")
                tag = f"url{len(self.last_urls)}{res['id']}{rt.index('end')}"
                rt.insert("end", "官網：")
                rt.insert("end", res["url"] + "\n\n", ("url", tag))
                rt.tag_bind(tag, "<Button-1>", lambda e, u=res["url"]: webbrowser.open(u))
            if skipped:
                rt.insert("end", "無法辨識的行程（已略過）：\n", "error")
                for s in skipped:
                    rt.insert("end", f"  ・{s}\n")
            self._plain_result = results_to_text(results, skipped)
            self.search_btn.config(state="normal", text="開始查詢")
            self.status_var.set(f"完成，共 {total} 筆維修資訊")

        # ---------- 其他 ----------
        def copy_result(self):
            content = getattr(self, "_plain_result", "") or self.result_text.get("1.0", "end").strip()
            if not content:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.status_var.set("已複製，可直接貼到 LINE")

        def open_all_urls(self):
            if not self.last_urls:
                messagebox.showinfo("提示", "請先執行查詢。")
                return
            for url in self.last_urls.values():
                webbrowser.open(url)

    root = tk.Tk()
    try:  # Windows 高解析度螢幕不模糊
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    MaintenanceApp(root)
    root.mainloop()


# ==============================================================================
# 8. 程式進入點
# ==============================================================================
def main():
    ap = argparse.ArgumentParser(description="日本樂園設施維修查詢工具")
    ap.add_argument("--export", metavar="PATH", help="匯出 JSON（給手機網頁版）")
    ap.add_argument("--days", type=int, default=240, help="匯出未來幾天的資料（預設 240）")
    ap.add_argument("--check", metavar="LINE", action="append",
                    help='不開視窗直接查詢，例如 --check "2027-01-17 ~ 2027-01-22 長島"')
    args = ap.parse_args()

    if args.export:
        export_json(args.export, days=args.days)
    elif args.check:
        tasks, skipped = [], []
        for line in args.check:
            d1, d2, c = parse_itinerary_line(line)
            (tasks.append((d1, d2, c)) if d1 and c else skipped.append(line))
        print(results_to_text(run_checks(tasks) if tasks else [], skipped))
    else:
        run_gui()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    main()
