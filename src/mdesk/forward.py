"""前瞻事件采集。

行情源只给已发生的公司行动，「上个月哪只票除过息」用处有限，
真正要的是「下周哪只票除息、哪只票出财报」。这些必须去官方或交易所源拿。

四个源，覆盖范围各不相同：
  1. Nasdaq 公开日历 API    美股未来除息日、登记日、派付日、公告日
  2. Nasdaq 拆股日历 API    美股未来拆合股生效日
  3. yfinance calendar      港美股下一次财报日
  4. 港交所披露易           港股公告原文，按标题分类，除息与业绩预告都在里面

披露易只给公告，不给结构化的除净日，需要人工点开 PDF 确认。
这是当前版本的已知局限，写在页面上而不是假装没有。
"""

import hashlib
import json
import time
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timedelta

import yfinance as yf

from . import db

warnings.filterwarnings("ignore")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"

NASDAQ_DIV = "https://api.nasdaq.com/api/calendar/dividends?date={d}"
NASDAQ_SPLIT = "https://api.nasdaq.com/api/calendar/splits?date={d}"
HKEX_PREFIX = ("https://www1.hkexnews.hk/search/prefix.do"
               "?callback=cb&lang=ZH&type=A&name={code}&market=SEHK")
HKEX_SEARCH = ("https://www1.hkexnews.hk/search/titleSearchServlet.do"
               "?sortDir=0&sortByOptions=DateTime&category=0&market=SEHK"
               "&stockId={sid}&documentType=-1&fromDate={f}&toDate={t}"
               # 分类的「全部」哨兵值是 -2 而不是 -1，写成 -1 会静默返回空结果
               "&title=&searchType=1&t1code=-1&t2Gcode=-2&t2code=-2"
               "&rowRange=100&lang=ZH")

# 披露易公告标题关键词到事件类型的映射。命中即分类，都不命中归为 other
HK_KEYWORDS = [
    ("ex_dividend", ["股息", "分派", "派息", "紅利"]),
    ("rights", ["供股", "配售", "認購", "供股權"]),
    ("split", ["股份合併", "股份拆細", "更改每手", "股本重組"]),
    ("earnings", ["業績", "中期報告", "年報", "季度業績", "盈利警告", "盈利預告"]),
    ("other", ["收購", "要約", "私有化", "更改公司名稱", "停牌", "復牌"]),
]


def _get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _eid(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _upsert(con, rows) -> None:
    if not rows:
        return
    con.executemany(
        """
        INSERT INTO events_calendar
            (event_id, symbol, event_date, event_type, title, url,
             source, fetched_at, payload, is_past)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (event_id) DO UPDATE SET
            event_date = excluded.event_date,
            title      = excluded.title,
            payload    = excluded.payload,
            is_past    = excluded.is_past,
            fetched_at = excluded.fetched_at
        """,
        rows,
    )


def _parse_us_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


# ----------------------------------------------------------------------

def fetch_nasdaq_dividends(con, wanted: set, days_ahead: int = 45) -> int:
    """逐日扫描 Nasdaq 除息日历，只留自选股。这是美股前瞻除息最完整的免费源。"""
    today = datetime.now().date()
    rows, scanned = [], 0
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:            # 周末不开市，跳过
            continue
        try:
            body = json.loads(_get(NASDAQ_DIV.format(d=d.isoformat())))
        except Exception:
            continue
        scanned += 1
        cal = (body.get("data") or {}).get("calendar") or {}
        for r in cal.get("rows") or []:
            sym = (r.get("symbol") or "").upper()
            if sym not in wanted:
                continue
            ex = _parse_us_date(r.get("dividend_Ex_Date") or "")
            if not ex:
                continue
            payload = json.dumps({
                "amount": r.get("dividend_Rate"),
                "record_date": r.get("record_Date"),
                "payment_date": r.get("payment_Date"),
                "announcement_date": r.get("announcement_Date"),
                "annual_dividend": r.get("indicated_Annual_Dividend"),
            }, ensure_ascii=False)
            rows.append((
                _eid("nasdaq_div", sym, ex), sym, ex, "ex_dividend",
                f"除息 每股 {r.get('dividend_Rate')}，登记日 {r.get('record_Date')}，"
                f"派付日 {r.get('payment_Date')}",
                "", "nasdaq", datetime.now(), payload, False,
            ))
        time.sleep(0.25)
    _upsert(con, rows)
    print(f"  Nasdaq 除息日历：扫描 {scanned} 个交易日，命中自选股 {len(rows)} 条")
    return len(rows)


def fetch_nasdaq_splits(con, wanted: set) -> int:
    """拆股日历一次返回全部待生效的拆股，不需要逐日扫。"""
    today = datetime.now().date()
    try:
        body = json.loads(_get(NASDAQ_SPLIT.format(d=today.isoformat())))
    except Exception as exc:
        print(f"  Nasdaq 拆股日历：失败 {exc}")
        return 0
    rows = []
    for r in (body.get("data") or {}).get("rows") or []:
        sym = (r.get("symbol") or "").upper()
        if sym not in wanted:
            continue
        eff = _parse_us_date(r.get("executionDate") or "")
        if not eff:
            continue
        rows.append((
            _eid("nasdaq_split", sym, eff), sym, eff, "split",
            f"拆股生效 比例 {r.get('ratio')}", "", "nasdaq",
            datetime.now(), json.dumps({"ratio": r.get("ratio")}, ensure_ascii=False),
            False,
        ))
    _upsert(con, rows)
    print(f"  Nasdaq 拆股日历：命中自选股 {len(rows)} 条")
    return len(rows)


def fetch_earnings_dates(con, symbols) -> int:
    """下一次财报日。港美股都能拿到，是覆盖面最广的一个前瞻字段。"""
    rows = []
    today = datetime.now().date()
    for sym in symbols:
        try:
            cal = yf.Ticker(sym).calendar or {}
        except Exception:
            continue
        dates = cal.get("Earnings Date") or []
        if not isinstance(dates, list):
            dates = [dates]
        for d in dates:
            if not hasattr(d, "year"):
                continue
            payload = json.dumps({
                "eps_estimate": cal.get("Earnings Average"),
                "revenue_estimate": cal.get("Revenue Average"),
            }, ensure_ascii=False, default=str)
            est = cal.get("Earnings Average")
            rows.append((
                _eid("earnings", sym, d), sym, d, "earnings",
                "财报" + (f"　市场预期 EPS {est}" if est else ""),
                "", "yfinance", datetime.now(), payload, d < today,
            ))
        time.sleep(0.2)
    _upsert(con, rows)
    upcoming = sum(1 for r in rows if not r[9])
    print(f"  财报日：{len(rows)} 条，其中未来 {upcoming} 条")
    return len(rows)


def _hk_stock_id(code4: str):
    try:
        raw = _get(HKEX_PREFIX.format(code=code4))
        raw = raw[raw.index("(") + 1: raw.rindex(")")]
        info = json.loads(raw).get("stockInfo") or []
        return info[0]["stockId"] if info else None
    except Exception:
        return None


def _classify_hk(title: str) -> str:
    for etype, words in HK_KEYWORDS:
        if any(w in title for w in words):
            return etype
    return "announcement"


def fetch_hkex_announcements(con, hk_symbols, days_back: int = 60) -> int:
    """港交所披露易公告。公告本身是前瞻的：八月发的股息公告说的是九月的除净日。

    局限：披露易只给公告标题和 PDF，除净日藏在正文里，需要人工点开确认。
    自动解析 PDF 不在本版范围内。
    """
    today = datetime.now().date()
    since = today - timedelta(days=days_back)
    rows, resolved = [], 0
    for sym in hk_symbols:
        code4 = sym.split(".")[0].zfill(5)[-5:]
        sid = _hk_stock_id(code4)
        if not sid:
            continue
        resolved += 1
        try:
            body = json.loads(_get(HKEX_SEARCH.format(
                sid=sid, f=since.strftime("%Y%m%d"), t=today.strftime("%Y%m%d"))))
            items = json.loads(body.get("result") or "[]")
        except Exception:
            continue
        for it in items:
            title = (it.get("TITLE") or "").strip()
            dt = it.get("DATE_TIME") or ""
            try:
                d = datetime.strptime(dt.split()[0], "%d/%m/%Y").date()
            except Exception:
                continue
            # 只按公告标题分类。早期版本把 HKEX 的分类描述也拼进来一起匹配，
            # 结果「H股激勵計劃」被归成供股，误报明显，改回只看标题。
            etype = _classify_hk(title)
            link = it.get("FILE_LINK") or ""
            url = f"https://www1.hkexnews.hk{link}" if link else ""
            rows.append((
                _eid("hkex", sym, it.get("NEWS_ID")), sym, d, etype,
                title, url, "hkexnews", datetime.now(),
                json.dumps({"category": it.get("LONG_TEXT")}, ensure_ascii=False),
                True,     # 公告日已过，但公告内容指向未来
            ))
        time.sleep(0.5)
    _upsert(con, rows)
    print(f"  披露易公告：解析 {resolved} 只港股，抓到 {len(rows)} 条公告")
    return len(rows)


def main() -> None:
    con = db.connect()
    con.execute(open_schema())
    wl = con.execute(
        "SELECT symbol, market FROM ref_watchlist WHERE NOT is_reference").fetchall()
    us = {s for s, m in wl if m == "US"}
    hk = [s for s, m in wl if m == "HK"]
    allsym = [s for s, _ in wl]

    print("前瞻事件采集")
    fetch_nasdaq_dividends(con, us)
    fetch_nasdaq_splits(con, us)
    fetch_earnings_dates(con, allsym)
    fetch_hkex_announcements(con, hk)

    today = datetime.now().date()
    n_future = con.execute(
        "SELECT count(*) FROM events_calendar WHERE event_date >= ?", [today]).fetchone()[0]
    print(f"\n未来事件共 {n_future} 条")
    for d, sym, et, title in con.execute(
        """
        SELECT event_date, symbol, event_type, title FROM events_calendar
        WHERE event_date >= ? ORDER BY event_date LIMIT 20
        """, [today]
    ).fetchall():
        print(f"  {d} {sym:10} {et:12} {title[:60]}")
    con.close()


def open_schema() -> str:
    from . import config
    return config.SCHEMA_SQL.read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
