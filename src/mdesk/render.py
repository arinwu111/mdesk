"""渲染静态站。页面只读 mart 层和 dq 层，不碰 raw。"""

import math
import shutil
import statistics
from datetime import datetime, date, timedelta

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import charts, config, db, report

S1 = "var(--series-1)"
S2 = "var(--series-2)"

ACTION_LABEL = {
    "cash_dividend": "现金派息", "split": "拆股", "reverse_split": "反向拆股",
    "spinoff": "分拆", "rights": "供股", "bonus": "红股",
    "scrip_dividend": "以股代息", "ratio_change": "存托比例变更",
}
EVENT_LABEL = {
    "earnings": "财报", "ex_dividend": "除息", "split": "拆股", "rights": "供股配售",
    "announcement": "一般公告", "halt": "停牌", "other": "其他",
}
STATUS_LABEL = {"open": "待查", "explained": "已解释", "fixed": "已修复",
                "ignored": "已忽略", "resolved": "已自动关闭"}
STATUS_BADGE = {"open": "open", "explained": "explained", "fixed": "ok",
                "ignored": "ok", "resolved": "ok"}
BADGE_LABEL = {"ok": "正常", "explained": "已解释", "open": "待查"}
MARKET_LABEL = {"HK": "港股", "US": "美股", "CN": "A 股", "TW": "台股"}
TYPE_LABEL = {"stock": "普通股", "etf": "交易所交易基金", "adr": "美国存托凭证", "sdr": "存托凭证"}
SEV_LABEL = {"P0": "当场处理", "P1": "记台账排期", "P2": "归档观察"}
DOMAIN_LABELS = [
    ("ref", "ref 主数据域"), ("price", "price 行情域"),
    ("corp_action", "corp_action 公司行动域"), ("fundamental", "fundamental 基本面域"),
    ("cross", "cross 跨市场域"), ("source", "source 跨源比对域"),
    ("ops", "ops 作业域"),
]


def slug(symbol: str) -> str:
    return symbol.replace(".", "-")


def num(v, digits=2, dash="—"):
    return dash if v is None else f"{v:,.{digits}f}"


def pct(v, dash="—"):
    return dash if v is None else f"{v:+.2f}%"


def money(v, dash="—"):
    if v is None:
        return dash
    if abs(v) >= 1e12:
        return f"{v / 1e12:,.2f} 万亿"
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.0f} 亿"
    return f"{v:,.0f}"


def direction(v):
    return "" if v is None else ("up" if v > 0 else ("down" if v < 0 else ""))


# ----------------------------------------------------------------------
# 各页面的数据组装
# ----------------------------------------------------------------------

def build_meta(con) -> dict:
    data_date = con.execute("SELECT max(trade_date) FROM price_primary").fetchone()[0]
    return {
        "data_date": data_date,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_symbols": con.execute(
            "SELECT count(*) FROM ref_watchlist WHERE NOT is_reference").fetchone()[0],
        "n_rules": con.execute("SELECT count(*) FROM dq_rules WHERE enabled").fetchone()[0],
        "n_open": con.execute(
            "SELECT count(*) FROM dq_results WHERE status = 'open'").fetchone()[0],
        "n_p0": con.execute(
            "SELECT count(*) FROM dq_results WHERE status = 'open' AND severity = 'P0'").fetchone()[0],
    }


BADGE_RANK = {"open": 2, "explained": 1, "ok": 0}

# 每只标的的单日跳空告警线 = 自身日波动率 × 6，与 DQ-PRC-002 的判据一致
SIGMA_K = 6


def _threshold(vol_ann):
    """把年化波动率折回单日告警线，页面上要显示的就是这个数。"""
    if not vol_ann:
        return None
    return max(vol_ann / math.sqrt(252) * SIGMA_K, 5.0)


def _volatility(con) -> dict:
    """近 120 个交易日的年化波动率。同时也是 DQ-PRC-002 的阈值依据。"""
    rows = con.execute(
        """
        WITH d AS (
            SELECT symbol, trade_date,
                   close / lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) - 1 AS ret
            FROM price_primary
            WHERE source = 'yfinance' AND close > 0
        ),
        recent AS (
            SELECT symbol, ret FROM d WHERE ret IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) <= 120
        )
        SELECT symbol, stddev_samp(ret) * sqrt(252) * 100 FROM recent
        GROUP BY 1 HAVING count(*) >= 30
        """
    ).fetchall()
    return dict(rows)


def page_index(con) -> dict:
    sparks = {}
    for sym, in con.execute(
            "SELECT DISTINCT symbol FROM mart_watchlist_snapshot").fetchall():
        vals = con.execute(
            "SELECT close FROM price_primary WHERE symbol = ?  "
            "ORDER BY trade_date DESC LIMIT 60", [sym]).fetchall()
        sparks[sym] = charts.sparkline([v[0] for v in reversed(vals)])
    vols = _volatility(con)

    rows = []
    for r in con.execute(
        """
        SELECT s.*, w.is_case
        FROM mart_watchlist_snapshot s JOIN ref_watchlist w ON w.symbol = s.symbol
        ORDER BY s.market, s.sector, s.symbol
        """
    ).fetchall():
        (symbol, code, name, market, sector, _td, close, _pc, chg, _vol,
         cur, mcap, pe, _pb, badge, summary, is_case) = r
        vol = vols.get(symbol)
        thr = _threshold(vol)
        # 角标为「正常」时也要说清楚判据，否则读者看到智谱涨 20% 却显示正常会以为出错
        if badge == "ok" and thr:
            tip = (f"今日 {chg:+.2f}%，未触及该标的告警线 ±{thr:.1f}%"
                   f"（= 其自身日波动率的 {SIGMA_K} 倍）" if chg is not None
                   else f"该标的告警线 ±{thr:.1f}%，今日无异常")
        else:
            tip = summary
        rows.append({
            "threshold": thr, "threshold_fmt": f"±{thr:.1f}%" if thr else "—",
            "symbol": symbol, "slug": slug(symbol), "display_code": code, "name": name,
            "market": market, "market_label": MARKET_LABEL.get(market, market),
            "sector": sector, "is_case": is_case,
            "close": close, "close_fmt": num(close, 3),
            "change": chg, "change_fmt": pct(chg), "dir": direction(chg),
            "spark": sparks.get(symbol, ""),
            "mcap": mcap, "mcap_fmt": money(mcap),
            "pe": pe, "pe_fmt": num(pe, 1),
            "vol": vol, "vol_fmt": f"{vol:.1f}%" if vol else "—",
            "badge": badge, "badge_label": BADGE_LABEL[badge],
            "badge_rank": BADGE_RANK.get(badge, 0), "dq_summary": tip,
        })

    counts = dict(con.execute(
        "SELECT market, count(*) FROM ref_watchlist WHERE NOT is_reference "
        "GROUP BY 1").fetchall())
    badge_counts = {"ok": 0, "explained": 0, "open": 0}
    badge_counts.update(dict(con.execute(
        "SELECT dq_badge, count(*) FROM mart_watchlist_snapshot GROUP BY 1").fetchall()))
    n_sectors = con.execute(
        "SELECT count(DISTINCT sector) FROM ref_watchlist WHERE NOT is_reference").fetchone()[0]

    # 说明文字里的例子直接取自真实数据，不写死，换了自选股也不会说错
    with_thr = sorted((r for r in rows if r["threshold"]), key=lambda r: r["threshold"])
    eg = {"high_thr": "—", "low_thr": "—",
          "high_name": "高波动标的", "low_name": "低波动标的"}
    if with_thr:
        hi_r, lo_r = with_thr[-1], with_thr[0]
        eg = {"high_thr": f"{hi_r['threshold']:.1f}%", "high_name": hi_r["name"],
              "low_thr": f"{lo_r['threshold']:.1f}%", "low_name": lo_r["name"]}

    return {"rows": rows, "counts": counts, "badge_counts": badge_counts,
            "n_sectors": n_sectors, "eg": eg}


def page_home(con, meta) -> dict:
    """总览首页。只回答一个问题：现在有没有需要动手的事。"""
    today = datetime.now().date()
    hour = datetime.now().hour
    greeting = ("早上好。" if hour < 11 else "下午好。" if hour < 18 else "晚上好。")

    # 不加 limit。这一页的定位是「有没有需要动手的事」，
    # 截断待办清单等于把该动手的事藏起来，与页面存在的理由直接冲突。
    todo = _results(con, "WHERE r.status = 'open' AND r.severity IN ('P0','P1')")

    # 时效性告警单独取出并置顶。数据本身过期时，页面上其余全部结论都建立在
    # 过期数据上，任何个股异常的优先级都低于它。
    blockers = _results(con, "WHERE r.status = 'open' AND r.rule_id = 'DQ-SYS-001'")
    todo = [t for t in todo if t["rule_id"] != "DQ-SYS-001"]

    soon = []
    for sym, code, name, ed, etype, title in con.execute(
        """
        SELECT e.symbol, w.display_code, w.name, e.event_date, e.event_type, e.title
        FROM events_calendar e JOIN ref_watchlist w ON w.symbol = e.symbol
        WHERE e.event_date >= ? AND e.event_date <= ? AND e.source <> 'hkexnews'
        ORDER BY e.event_date LIMIT 12
        """, [today, today + timedelta(days=14)]
    ).fetchall():
        d = (ed - today).days
        soon.append({
            "slug": slug(sym), "name": name, "event_date": ed, "days": d,
            "days_fmt": "今天" if d == 0 else f"{d} 天",
            "type_label": EVENT_LABEL.get(etype, etype), "title": title,
        })

    # 按角标严重度排序，异常项全部展开，只有无异常的标的允许折叠。
    # 原实现取涨跌幅前四后四、中间省略，结果是带红黄角标的标的可能正好落在
    # 被省略的那一段里，页面看起来干净但该动手的事看不到。
    markets = []
    for mk, label in (("HK", "港股"), ("US", "美股")):
        rs = con.execute(
            """
            SELECT symbol, name, sector, change_pct, trade_date, dq_badge, dq_summary
            FROM mart_watchlist_snapshot
            WHERE market = ? AND change_pct IS NOT NULL
            ORDER BY CASE dq_badge WHEN 'open' THEN 0 WHEN 'explained' THEN 1 ELSE 2 END,
                     abs(change_pct) DESC
            """, [mk]).fetchall()
        if not rs:
            continue

        def fmt(r):
            return {"slug": slug(r[0]), "name": r[1], "sector": r[2],
                    "change_fmt": pct(r[3]), "dir": direction(r[3]),
                    "badge": r[5], "badge_label": BADGE_LABEL.get(r[5], ""),
                    "summary": r[6]}

        flagged = [r for r in rs if r[5] in ("open", "explained")]
        clean = [r for r in rs if r[5] not in ("open", "explained")]
        markets.append({
            "label": label, "n": len(rs), "trade_date": rs[0][4],
            "n_up": sum(1 for r in rs if r[3] > 0),
            "n_down": sum(1 for r in rs if r[3] < 0),
            "flagged": [fmt(r) for r in flagged],
            "clean": [fmt(r) for r in clean],
            "n_flagged": len(flagged), "n_clean": len(clean),
        })

    fail = con.execute(
        "SELECT count(*) FROM raw_fetch_log WHERE run_id = (SELECT max(run_id) "
        "FROM raw_fetch_log) AND status = 'error'").fetchone()[0]
    # 时效性口径统一由 DQ-SYS-001 判定，这里只取它有没有命中，
    # 不再自己按自然日算一遍，避免两处阈值各自漂移。
    stale_days = con.execute(
        """
        SELECT count(*) FROM generate_series(
                 (SELECT max(trade_date) FROM price_primary) + 1,
                 CURRENT_DATE - 1, INTERVAL 1 DAY) g(d)
        WHERE isodow(g.d::DATE) BETWEEN 1 AND 5
        """).fetchone()[0]

    return {"home": {
        "greeting": greeting,
        "p0": con.execute("SELECT count(*) FROM dq_results WHERE status = 'open' "
                          "AND severity = 'P0'").fetchone()[0],
        "p1": con.execute("SELECT count(*) FROM dq_results WHERE status = 'open' "
                          "AND severity = 'P1'").fetchone()[0],
        "n_ok": con.execute("SELECT count(*) FROM mart_watchlist_snapshot "
                            "WHERE dq_badge <> 'open'").fetchone()[0],
        "n_soon": sum(1 for e in soon if e["days"] <= 3),
        "todo": todo, "soon": soon, "markets": markets,
        "blockers": blockers,
        "fetch_label": ("过期" if blockers else "正常" if fail == 0 else f"{fail} 项失败"),
        "fetch_sub": (f"最新交易日落后 {stale_days} 个工作日" if blockers
                      else "最近一轮全部成功" if fail == 0
                      else "见异常清单 DQ-OPS-001"),
    }}


def _actions_for(con, symbol):
    out = []
    for ex_date, atype, ratio, amount, cur, note in con.execute(
        "SELECT ex_date, action_type, ratio, amount, currency, note "
        "FROM events_corporate_action WHERE symbol = ? ORDER BY ex_date DESC", [symbol]
    ).fetchall():
        prev = con.execute(
            "SELECT close FROM price_primary WHERE symbol = ? AND trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1", [symbol, ex_date]).fetchone()
        prev_close = prev[0] if prev else None
        p = (amount / prev_close * 100) if (amount and prev_close) else None
        out.append({
            "ex_date": ex_date, "type_label": ACTION_LABEL.get(atype, atype),
            "amount_fmt": f"{amount:.4f} {cur or ''}".strip() if amount else "—",
            "ratio_fmt": num(ratio, 4), "prev_fmt": num(prev_close, 3),
            "pct_fmt": f"{p:.2f}%" if p is not None else "—", "note": note or "",
        })
    return out


def page_stock(con, symbol, vols_all=None) -> dict:
    vols_all = vols_all or {}
    (code, name, market, sector, itype, is_case, case_note, is_ref) = con.execute(
        "SELECT display_code, name, market, sector, instrument_type, is_case, case_note, "
        "is_reference FROM ref_watchlist WHERE symbol = ?", [symbol]).fetchone()
    snap = con.execute(
        "SELECT trade_date, close, prev_close, change_pct, currency, market_cap, pe_ttm, "
        "dq_badge FROM mart_watchlist_snapshot WHERE symbol = ?", [symbol]).fetchone()
    if snap is None:
        # 对照标的不进自选股快照，但复权核对页会链接过来，所以直接从原始行情兜底
        snap = con.execute(
            """
            SELECT p.trade_date, p.close,
                   lag(p.close) OVER (ORDER BY p.trade_date), NULL,
                   p.currency, f.market_cap, f.pe_ttm, 'ok'
            FROM price_primary p
            LEFT JOIN raw_fundamental f ON f.symbol = p.symbol
            WHERE p.symbol = ?
            QUALIFY row_number() OVER (ORDER BY p.trade_date DESC) = 1
            """, [symbol]).fetchone()
    trade_date, close, prev_close, chg, cur, mcap, pe, badge = snap or (None,) * 8
    if chg is None and close and prev_close:
        chg = (close / prev_close - 1) * 100

    series_rows = con.execute(
        """
        SELECT m.trade_date, m.close, m.self_adj, m.src_adj, m.diff_pct, p.volume
        FROM mart_adj_price m
        LEFT JOIN price_primary p
               ON p.symbol = m.symbol AND p.trade_date = m.trade_date
             
        WHERE m.symbol = ? ORDER BY m.trade_date
        """, [symbol]).fetchall()
    x = [str(r[0])[2:7] for r in series_rows]
    has_diff = any(r[4] is not None and abs(r[4]) > 0.5 for r in series_rows)
    if has_diff:
        series = [
            {"name": "自算复权价", "color": S1, "values": [r[2] for r in series_rows]},
            {"name": "数据源复权价", "color": S2, "values": [r[3] for r in series_rows]},
        ]
    else:
        series = [{"name": "收盘价", "color": S1, "values": [r[1] for r in series_rows]}]

    issues = [
        {"biz_date": b, "rule_id": rid, "severity": sev, "detail": d,
         "status_label": STATUS_LABEL.get(st, st), "handling_note": hn}
        for rid, b, sev, d, st, hn in con.execute(
            "SELECT rule_id, biz_date, severity, detail, status, handling_note "
            "FROM dq_results WHERE symbol = ? ORDER BY severity, biz_date DESC", [symbol]
        ).fetchall()
    ]
    eps = con.execute(
        "SELECT eps_ttm FROM raw_fundamental WHERE symbol = ? "
        "ORDER BY snapshot_date DESC LIMIT 1", [symbol]).fetchone()
    eps = eps[0] if eps else None
    if pe is not None:
        pe_note = "有盈利"
    elif itype == "etf":
        pe_note = "ETF 不适用"
    elif eps is not None and eps <= 0:
        pe_note = "无盈利，指标不适用"
    else:
        pe_note = "字段缺失，已告警"

    # ---- 区间统计 ----
    closes = [(r[0], r[1]) for r in series_rows]
    stats = []
    for label, days in (("近 1 月", 21), ("近 3 月", 63), ("近 1 年", 252), ("全区间", len(closes))):
        if len(closes) > days >= 1:
            base = closes[-days - 1][1] if len(closes) > days else closes[0][1]
        elif closes:
            base = closes[0][1]
        else:
            continue
        last = closes[-1][1]
        r = (last / base - 1) * 100 if base else None
        stats.append({"label": label, "value": pct(r), "dir": direction(r)})

    rets = [closes[i][1] / closes[i - 1][1] - 1
            for i in range(1, len(closes)) if closes[i - 1][1]]
    recent = rets[-120:]
    vol_ann = (statistics.stdev(recent) * math.sqrt(252) * 100
               if len(recent) >= 30 else None)
    peak, mdd = None, 0.0
    for _d, c in closes:
        peak = c if peak is None or c > peak else peak
        if peak:
            mdd = min(mdd, c / peak - 1)
    hi = max((c for _d, c in closes), default=None)
    lo = min((c for _d, c in closes), default=None)

    # ---- 关键指标 ----
    last = con.execute(
        """
        SELECT open, high, low, close, volume FROM price_primary
        WHERE symbol = ? 
        QUALIFY row_number() OVER (ORDER BY trade_date DESC) = 1
        """, [symbol]).fetchone()
    fund = con.execute(
        """
        SELECT shares_outstanding, pb, ps_ttm, dividend_yield, eps_ttm
        FROM raw_fundamental WHERE symbol = ?
        QUALIFY row_number() OVER (ORDER BY snapshot_date DESC) = 1
        """, [symbol]).fetchone() or (None,) * 5
    shares, pb, ps, dy, _eps = fund

    cl = [c for _d, c in closes]
    vols_hist = [r[5] for r in series_rows if r[5]]

    def ma(n):
        return sum(cl[-n:]) / n if len(cl) >= n else None

    def dev(n):
        m = ma(n)
        return (cl[-1] / m - 1) * 100 if m and cl else None

    w52 = cl[-252:] if len(cl) >= 252 else cl
    hi52, lo52 = (max(w52), min(w52)) if w52 else (None, None)
    avg_vol5 = (sum(vols_hist[-6:-1]) / 5) if len(vols_hist) >= 6 else None
    o, h, lo_, c, v = last or (None,) * 5

    turnover = (v / shares * 100) if (v and shares) else None
    ind = [
        {"k": "换手率", "v": f"{turnover:.2f}%" if turnover else "—",
         "n": "成交量 ÷ 总股本"},
        {"k": "量比", "v": f"{v / avg_vol5:.2f}" if (v and avg_vol5) else "—",
         "n": "今日量 ÷ 前 5 日均量"},
        {"k": "振幅", "v": f"{(h - lo_) / prev_close * 100:.2f}%"
                          if (h and lo_ and prev_close) else "—", "n": "(最高−最低) ÷ 前收"},
        {"k": "成交额", "v": f"{charts._human(c * v)} {cur or ''}" if (c and v) else "—",
         "n": "收盘价 × 成交量，估算值"},
        {"k": "52 周最高", "v": num(hi52, 3), "n": f"距今 {(c / hi52 - 1) * 100:+.1f}%"
                                                  if (c and hi52) else ""},
        {"k": "52 周最低", "v": num(lo52, 3), "n": f"距今 {(c / lo52 - 1) * 100:+.1f}%"
                                                  if (c and lo52) else ""},
        {"k": "MA5", "v": num(ma(5), 3), "n": f"乖离 {dev(5):+.2f}%" if dev(5) else ""},
        {"k": "MA20", "v": num(ma(20), 3), "n": f"乖离 {dev(20):+.2f}%" if dev(20) else ""},
        {"k": "MA60", "v": num(ma(60), 3), "n": f"乖离 {dev(60):+.2f}%" if dev(60) else ""},
        {"k": "市净率 PB", "v": num(pb, 2), "n": "数据源提供"},
        {"k": "市销率 PS", "v": num(ps, 2), "n": "TTM"},
        {"k": "股息率", "v": f"{dy:.2f}%" if dy else "—", "n": "数据源提供"},
        {"k": "总股本", "v": charts._human(shares) if shares else "—", "n": "股"},
    ]
    # A+H 与双重上市标的的总股本口径会把另一地的股份算进来，换手率因此失真
    ah = con.execute(
        "SELECT count(*) FROM ref_cross_listing WHERE symbol_a = ? OR symbol_b = ?",
        [symbol, symbol]).fetchone()[0]

    # ---- 同行业对比 ----
    peers = []
    for psym, pname, pchg, pbadge in con.execute(
        """
        SELECT symbol, name, change_pct, dq_badge FROM mart_watchlist_snapshot
        WHERE sector = ? ORDER BY change_pct DESC NULLS LAST
        """, [sector]
    ).fetchall():
        peers.append({
            "slug": slug(psym), "name": pname, "change_fmt": pct(pchg),
            "dir": direction(pchg), "is_self": psym == symbol,
            "badge": pbadge, "badge_label": BADGE_LABEL.get(pbadge, ""),
            "vol_fmt": (f"{vols_all[psym]:.1f}%" if vols_all.get(psym) else "—"),
        })

    return {"s": {
        "symbol": symbol, "display_code": code, "name": name, "sector": sector,
        "market_label": MARKET_LABEL.get(market, market),
        "type_label": TYPE_LABEL.get(itype, itype),
        "case_note": case_note if (is_case or is_ref) else "",
        "is_reference": is_ref,
        "trade_date": trade_date, "currency": cur or "",
        "close_fmt": num(close, 3), "prev_fmt": num(prev_close, 3),
        "change_fmt": pct(chg), "dir": direction(chg),
        "mcap_fmt": money(mcap), "pe_fmt": num(pe, 1), "pe_note": pe_note,
        "badge": badge or "ok", "badge_label": BADGE_LABEL.get(badge or "ok"),
        "open_count": sum(1 for i in issues if i["status_label"] == "待查"),
        "two_series": has_diff, "n_points": len(series_rows),
        "chart": charts.line_chart(x, series),
        "vol_chart": charts.volume_chart(x, [r[5] for r in series_rows]),
        "actions": _actions_for(con, symbol), "issues": issues,
        "stats": stats,
        "vol_ann": f"{vol_ann:.1f}%" if vol_ann else "—",
        "threshold": f"{vol_ann / math.sqrt(252) * 6:.1f}%" if vol_ann else "—",
        "mdd": f"{mdd * 100:.1f}%" if mdd else "—",
        "hi_fmt": num(hi, 3), "lo_fmt": num(lo, 3),
        "peers": peers, "n_peers": len(peers),
        "indicators": ind, "is_cross_listed": ah > 0,
    }}


IMPLIED_SQL = """
WITH p AS (
    SELECT symbol, trade_date, close, adj_close, adj_close / close AS ratio,
           lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close,
           lag(adj_close / close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_ratio
    FROM price_primary WHERE close > 0 AND adj_close > 0
)
SELECT ca.symbol, w.display_code, w.name, p.trade_date AS ex_date,
       ca.amount, ca.currency, p.prev_close,
       p.prev_close * (1 - p.prev_ratio / p.ratio) AS implied
FROM p
JOIN events_corporate_action ca ON ca.symbol = p.symbol AND ca.ex_date = p.trade_date
                               AND ca.action_type = 'cash_dividend'
JOIN ref_watchlist w ON w.symbol = ca.symbol
WHERE ca.amount IS NOT NULL AND p.prev_ratio IS NOT NULL
ORDER BY p.trade_date DESC
"""


def page_corpaction(con) -> dict:
    rows, bad_syms = [], set()
    for sym, code, name, ex_date, amount, cur, prev_close, implied in con.execute(
            IMPLIED_SQL).fetchall():
        mult = amount / implied if implied and implied > 0 else None
        bad = mult is not None and abs(mult - 1) > 0.01
        if bad:
            bad_syms.add(sym)
        rows.append({
            "symbol": sym, "slug": slug(sym), "display_code": code, "name": name,
            "ex_date": ex_date, "amount_fmt": num(amount, 6), "currency": cur or "",
            "prev_fmt": num(prev_close, 3), "implied_fmt": num(implied, 6),
            "mult_fmt": num(mult, 4), "bad": bad,
        })

    affected = []
    for sym in sorted(bad_syms):
        code, name = con.execute(
            "SELECT display_code, name FROM ref_watchlist WHERE symbol = ?", [sym]).fetchone()
        data = con.execute(
            "SELECT trade_date, self_adj, src_adj, diff_pct FROM mart_adj_price "
            "WHERE symbol = ? ORDER BY trade_date", [sym]).fetchall()
        n_off = sum(1 for d in data if d[3] is not None and abs(d[3]) > 0.5)
        max_off = max((abs(d[3]) for d in data if d[3] is not None), default=0)
        affected.append({
            "name": name, "display_code": code,
            "chart": charts.line_chart(
                [str(d[0])[2:7] for d in data],
                [{"name": "自算复权价", "color": S1, "values": [d[1] for d in data]},
                 {"name": "数据源复权价", "color": S2, "values": [d[2] for d in data]}],
                height=260),
            "caption": (f"{len(data)} 个交易日中有 {n_off} 日偏离超过 0.5%，最大偏离 {max_off:.3f}%。"
                        "两条线本应完全重合，分岔点即为出问题的除权日。"),
        })

    affected_days = con.execute(
        "SELECT count(*) FROM mart_adj_price WHERE diff_pct IS NOT NULL "
        "AND abs(diff_pct) > 0.5").fetchone()[0]
    bad_n = sum(1 for r in rows if r["bad"])
    return {
        "rows": rows, "affected": affected,
        "stats": {"total": len(rows), "ok": len(rows) - bad_n, "bad": bad_n,
                  "symbols": len({r["symbol"] for r in rows}),
                  "affected_days": affected_days},
        "finding": FINDING_HTML,
    }


FINDING_HTML = """
<p style="margin-top:0"><b>数据源的派息金额与其自身的复权价互不自洽，出现在两只有美股 ADR 的港股上。</b></p>
<p>阿里巴巴 9988.HK 的两次派息全部对不上，汇丰控股 0005.HK 九次派息中最近一次对不上。
三笔的隐含倍数分别是 7.847、7.847、7.847，完全一致。</p>
<p>7.847 正好是港币兑美元联系汇率的倒数。也就是说，数据源上报的每股派息是港币金额（正确），
但它计算复权价时扣减的是美元金额。阿里每份 ADS 派息 2.00 美元、对应 8 股港股，
每股 0.25 美元；数据源从 118.80 港币的价格里扣掉了 0.25，而不是 1.96 港币。</p>
<p>其余 21 只有派息记录的标的倍数全部为 1.0000，包括腾讯、中国石油这类纯港股，
以及阿里自己的美股 BABA。问题只出在同时有美股 ADR 的港股标的上。</p>
<p style="margin-bottom:0"><b>待验证的假设：</b>汇丰只有最近一次（2026-08-13，三天前）出错，
更早八次都是对的，说明这可能是新派息入库时的临时状态，过一段时间会自行修正。
这个假设需要在两周后复检，结论记入处理台账。</p>
"""


def page_calendar(con) -> dict:
    flagged = {(s, d) for s, d in con.execute(
        "SELECT symbol, biz_date FROM dq_results WHERE rule_id = 'DQ-CA-001'").fetchall()}
    prev_map = {}
    all_rows = []
    for sym, code, name, ex_date, atype, ratio, amount, cur in con.execute(
        """
        SELECT ca.symbol, w.display_code, w.name, ca.ex_date, ca.action_type,
               ca.ratio, ca.amount, ca.currency
        FROM events_corporate_action ca JOIN ref_watchlist w ON w.symbol = ca.symbol
        WHERE NOT w.is_reference ORDER BY ca.ex_date DESC
        """
    ).fetchall():
        key = (sym, ex_date)
        if key not in prev_map:
            r = con.execute(
                "SELECT close FROM price_primary WHERE symbol = ? AND trade_date < ? "
                "ORDER BY trade_date DESC LIMIT 1", [sym, ex_date]).fetchone()
            prev_map[key] = r[0] if r else None
        prev_close = prev_map[key]
        p = (amount / prev_close * 100) if (amount and prev_close) else None
        all_rows.append({
            "symbol": sym, "slug": slug(sym), "display_code": code, "name": name,
            "ex_date": ex_date, "type_label": ACTION_LABEL.get(atype, atype),
            "amount_fmt": f"{amount:.4f} {cur or ''}".strip() if amount else "—",
            "ratio_fmt": num(ratio, 4),
            "pct_fmt": f"{p:.2f}%" if p is not None else "—",
            "flagged": key in flagged,
        })

    latest = con.execute("SELECT max(trade_date) FROM price_primary").fetchone()[0]
    cutoff = date(latest.year - 1, latest.month, latest.day)
    recent = [r for r in all_rows if r["ex_date"] >= cutoff]
    older = [r for r in all_rows if r["ex_date"] < cutoff]
    div = [r for r in all_rows if "派息" in r["type_label"]]

    # ---- 前瞻事件 ----
    today = datetime.now().date()
    upcoming = []
    for sym, code, name, ed, etype, title, src in con.execute(
        """
        SELECT e.symbol, w.display_code, w.name, e.event_date, e.event_type,
               e.title, e.source
        FROM events_calendar e JOIN ref_watchlist w ON w.symbol = e.symbol
        WHERE e.event_date >= ? AND e.source <> 'hkexnews'
        ORDER BY e.event_date
        """, [today]
    ).fetchall():
        d = (ed - today).days
        upcoming.append({
            "symbol": sym, "slug": slug(sym), "display_code": code, "name": name,
            "event_date": ed, "days": d,
            "days_fmt": "今天" if d == 0 else f"{d} 天",
            "type_label": EVENT_LABEL.get(etype, etype), "title": title, "source": src,
        })

    hk_notices = [
        {"slug": slug(sym), "display_code": code, "name": name, "event_date": ed,
         "type_label": EVENT_LABEL.get(etype, etype), "title": title, "url": url}
        for sym, code, name, ed, etype, title, url in con.execute(
            """
            SELECT e.symbol, w.display_code, w.name, e.event_date, e.event_type,
                   e.title, e.url
            FROM events_calendar e JOIN ref_watchlist w ON w.symbol = e.symbol
            WHERE e.source = 'hkexnews'
            ORDER BY (e.event_type <> 'announcement') DESC, e.event_date DESC
            LIMIT 80
            """
        ).fetchall()
    ]
    n_hk = con.execute(
        "SELECT count(*) FROM events_calendar WHERE source = 'hkexnews'").fetchone()[0]
    n_hk_key = con.execute(
        "SELECT count(*) FROM events_calendar WHERE source = 'hkexnews' "
        "AND event_type IN ('ex_dividend', 'rights', 'split')").fetchone()[0]
    nxt = upcoming[0] if upcoming else None

    return {
        "recent": recent, "older": older,
        "upcoming": upcoming, "hk_notices": hk_notices,
        "fwd": {
            # 分项必须和总数同口径，都限定在 30 天内
            "n_30": sum(1 for e in upcoming if e["days"] <= 30),
            "n_earn": sum(1 for e in upcoming if e["days"] <= 30 and e["type_label"] == "财报"),
            "n_div": sum(1 for e in upcoming if e["days"] <= 30 and e["type_label"] == "除息"),
            "n_split": sum(1 for e in upcoming if e["days"] <= 30 and e["type_label"] == "拆股"),
            "next_date": nxt["event_date"] if nxt else "—",
            "next_name": nxt["name"] if nxt else "",
            "next_desc": f"{nxt['type_label']}　{nxt['days_fmt']}" if nxt else "",
            "n_hk": n_hk, "n_hk_key": n_hk_key,
        },
        "stats": {
            "total": len(all_rows), "symbols": len({r["symbol"] for r in all_rows}),
            "dividends": len(div), "div_symbols": len({r["symbol"] for r in div}),
            "splits": len(all_rows) - len(div),
            "latest_date": all_rows[0]["ex_date"] if all_rows else "—",
            "latest_name": all_rows[0]["name"] if all_rows else "—",
        },
    }


def page_newlisting(con) -> dict:
    latest = con.execute("SELECT max(trade_date) FROM price_primary").fetchone()[0]
    # 各市场交易日历不同，窗口起点必须按市场分别取，否则会把日历差当成新上市
    market_start = dict(con.execute(
        """
        SELECT w.market, min(p.trade_date)
        FROM price_primary p JOIN ref_watchlist w ON w.symbol = p.symbol
        GROUP BY 1
        """
    ).fetchall())
    window_start = min(market_start.values())
    rows = []
    for sym, code, name, market, sector in con.execute(
        "SELECT symbol, display_code, name, market, sector FROM ref_watchlist "
        "WHERE NOT is_reference"
    ).fetchall():
        r = con.execute(
            "SELECT min(trade_date), max(trade_date), count(*) FROM price_primary "
            "WHERE symbol = ?", [sym]).fetchone()
        if not r or not r[0]:
            continue
        start, _end, n = r
        first = con.execute(
            "SELECT close FROM price_primary WHERE symbol = ? ORDER BY trade_date LIMIT 1",
            [sym]).fetchone()[0]
        snap = con.execute(
            "SELECT close, pe_ttm, dq_badge FROM mart_watchlist_snapshot WHERE symbol = ?",
            [sym]).fetchone()
        close, pe, badge = snap or (None, None, "ok")
        total = (close / first - 1) * 100 if (close and first) else None
        rows.append({
            "symbol": sym, "slug": slug(sym), "display_code": code, "name": name,
            "market": MARKET_LABEL.get(market, market), "sector": sector,
            "start": start, "n_days": n, "close_fmt": num(close, 3),
            "pe_fmt": num(pe, 1), "total_fmt": pct(total), "dir": direction(total),
            "badge": badge, "badge_label": BADGE_LABEL[badge], "_pe": pe,
            "_new": (r[0] - market_start.get(market, window_start)).days > 5,
        })
    rows.sort(key=lambda r: r["start"], reverse=True)
    new_rows = [r for r in rows if r["_new"]]
    year_ago = date(latest.year - 1, latest.month, latest.day)
    return {
        "rows": rows,
        "stats": {
            "n_new": len(new_rows), "window_start": window_start,
            "n_year": sum(1 for r in rows if r["start"] > year_ago),
            "n_missing": sum(1 for r in rows if r["_pe"] is None),
            "newest_name": rows[0]["name"] if rows else "—",
            "newest_date": rows[0]["start"] if rows else "—",
        },
    }


def _results(con, where="", params=None, limit=None):
    sql = f"""
        SELECT r.rule_id, d.name, r.symbol, coalesce(w.name, r.symbol) AS name2,
               w.is_reference, r.biz_date, r.severity, r.detail, r.status,
               r.first_seen, r.handled_at, r.handling_note
        FROM dq_results r
        LEFT JOIN ref_watchlist w ON w.symbol = r.symbol
        LEFT JOIN dq_rules d ON d.rule_id = r.rule_id
        {where}
        ORDER BY r.severity, r.biz_date DESC, r.rule_id
        {f'LIMIT {limit}' if limit else ''}
    """
    out = []
    for (rid, rname, sym, name, is_ref, bdate, sev, detail, st,
         first, handled, hnote) in con.execute(sql, params or []).fetchall():
        out.append({
            "rule_id": rid, "rule_name": rname or "", "symbol": sym, "name": name,
            "slug": slug(sym) if (is_ref is False) else None,
            "biz_date": bdate, "severity": sev, "detail": detail,
            "status": st, "status_label": STATUS_LABEL.get(st, st),
            "badge": STATUS_BADGE.get(st, "open"),
            "first_seen": str(first)[:10] if first else "",
            "handled_at": str(handled)[:16] if handled else None,
            "handling_note": hnote,
        })
    return out


def _rule_hits(con):
    return dict(con.execute(
        "SELECT rule_id, count(*) FROM dq_results GROUP BY 1").fetchall())


def page_ops_index(con) -> dict:
    rows = _results(con, "WHERE r.status = 'open'")
    groups = {s: [r for r in rows if r["severity"] == s] for s in ("P0", "P1", "P2")}
    hits = _rule_hits(con)
    rule_rows = con.execute(
        "SELECT rule_id, name FROM dq_rules WHERE enabled ORDER BY rule_id").fetchall()
    bars = sorted(
        [(f"{rid} {nm}", hits.get(rid, 0), f"{rid} {nm}：命中 {hits.get(rid, 0)} 条")
         for rid, nm in rule_rows],
        key=lambda t: -t[1])
    handled = con.execute(
        "SELECT count(*) FROM dq_results WHERE status <> 'open'").fetchone()[0]
    return {
        "groups": groups, "sev_label": SEV_LABEL,
        "counts": {"P0": len(groups["P0"]), "P1": len(groups["P1"]),
                   "P2": len(groups["P2"]), "handled": handled,
                   "hit_rules": sum(1 for _, v, _ in bars if v)},
        "hit_chart": charts.bar_chart(bars, row_h=24, label_w=290),
    }


def page_rules(con) -> dict:
    hits = _rule_hits(con)
    by_domain = {d: [] for d, _ in DOMAIN_LABELS}
    all_rules = con.execute(
        "SELECT rule_id, domain, name, description, severity, created_at, note "
        "FROM dq_rules WHERE enabled ORDER BY rule_id").fetchall()
    for rid, domain, name, desc, sev, created, note in all_rules:
        n = hits.get(rid, 0)
        by_domain.setdefault(domain, []).append({
            "rule_id": rid, "name": name, "description": desc, "severity": sev,
            "created_at": created, "note": note,
            "hit_label": f"命中 {n} 条" if n else "本轮无命中",
        })
    domains = [d for d, _ in DOMAIN_LABELS if by_domain.get(d)]
    return {
        "by_domain": by_domain, "domain_labels": DOMAIN_LABELS,
        "stats": {
            "total": len(all_rules),
            "hit": sum(1 for r in all_rules if hits.get(r[0], 0)),
            "domains": len(domains), "domain_list": " · ".join(domains),
            "p0": sum(1 for r in all_rules if r[4] == "P0"),
        },
    }


def page_ledger(con) -> dict:
    rows = _results(con, limit=400)
    counts = dict(con.execute(
        "SELECT status, count(*) FROM dq_results GROUP BY 1").fetchall())
    total = sum(counts.values())
    return {
        "rows": rows, "truncated": total > len(rows),
        "stats": {"total": total, "open": counts.get("open", 0),
                  "explained": counts.get("explained", 0),
                  "fixed": counts.get("fixed", 0), "ignored": counts.get("ignored", 0)},
    }


def page_report(con, meta) -> dict:
    n_price = con.execute("SELECT count(*) FROM price_primary").fetchone()[0]
    n_actions = con.execute("SELECT count(*) FROM events_corporate_action").fetchone()[0]
    sev = dict(con.execute("SELECT severity, count(*) FROM dq_results GROUP BY 1").fetchall())
    n_issues = sum(sev.values())
    fetch_ok, fetch_total = con.execute(
        "SELECT count(*) FILTER (WHERE status = 'ok'), count(*) FROM raw_fetch_log "
        "WHERE run_id = (SELECT max(run_id) FROM raw_fetch_log)").fetchone()

    domain_bars = sorted(
        [(f"{d}（{lbl.split()[1] if ' ' in lbl else lbl}）", n, f"{d} 域命中 {n} 条")
         for d, lbl in DOMAIN_LABELS
         for n in [con.execute(
             "SELECT count(*) FROM dq_results r JOIN dq_rules u ON u.rule_id = r.rule_id "
             "WHERE u.domain = ?", [d]).fetchone()[0]]],
        key=lambda t: -t[1])

    symbol_bars = sorted(
        [(f"{name}", n, f"{name} 命中 {n} 条")
         for name, n in con.execute(
             "SELECT coalesce(w.name, r.symbol), count(*) FROM dq_results r "
             "LEFT JOIN ref_watchlist w ON w.symbol = r.symbol GROUP BY 1"
         ).fetchall()],
        key=lambda t: -t[1])

    hits = _rule_hits(con)
    iterations = [
        {"rule_id": rid, "name": nm, "severity": sv, "hits": hits.get(rid, 0), "note": note}
        for rid, nm, sv, note in con.execute(
            "SELECT rule_id, name, severity, note FROM dq_rules WHERE enabled ORDER BY rule_id"
        ).fetchall()
    ]
    latest = con.execute("SELECT min(trade_date), max(trade_date) FROM price_primary").fetchone()

    m = report.metrics(con)
    return {
        "m": m,
        "period": f"{latest[0]} 至 {latest[1]}（首期，覆盖全部采集窗口）",
        "s": {
            "fetch_rate": f"{fetch_ok / fetch_total * 100:.0f}%" if fetch_total else "—",
            "fetch_ok": fetch_ok, "fetch_total": fetch_total,
            "n_price": f"{n_price:,}", "n_actions": n_actions, "n_issues": n_issues,
            "p0": sev.get("P0", 0), "p1": sev.get("P1", 0), "p2": sev.get("P2", 0),
            "issue_rate": f"{n_issues / n_price * 100:.3f}%" if n_price else "—",
        },
        "domain_chart": charts.bar_chart(domain_bars, row_h=26, label_w=210),
        "symbol_chart": charts.bar_chart(symbol_bars, row_h=24, label_w=210),
        "findings": REPORT_FINDINGS, "iterations": iterations, "gaps": REPORT_GAPS,
    }


REPORT_FINDINGS = [
    {"severity": "P0", "title": "数据源派息金额与其复权价不自洽", "scope": "9988.HK、0005.HK 共 3 笔",
     "body": "阿里巴巴两次派息全部不自洽，汇丰最近一次不自洽，隐含倍数均为 7.847，"
             "等于港币兑美元联系汇率。数据源用美元金额扣减了港币价格。"
             "两只中招的都是同时有美股 ADR 的港股，纯港股和纯美股标的全部自洽。"
             "<b>处理</b>：复权价一律以自算为准，不使用数据源的 Adj Close。"
             "<b>待办</b>：两周后复检汇丰，验证「新派息临时错、事后自动修正」的假设。"},
    {"severity": "P1", "title": "阿里巴巴历史复权价整段偏低", "scope": "445 个交易日",
     "body": "受上述问题影响，阿里全历史 445 个交易日的复权价偏低最多 2.201%。"
             "任何基于数据源复权价计算的历史涨跌幅、区间收益、波动率都会有系统性偏差。"
             "<b>处理</b>：该标的的历史分析全部改用 mart_adj_price 的自算列。"},
    {"severity": "P2", "title": "港股半日市当日成交量整体缺失", "scope": "6 个交易日",
     "body": "平安夜、除夕、农历新年前一日共 6 天，港股标的成交量集体为零。"
             "港交所这几天只有早市，数据源不返回成交量。这属于已知市场规则而非数据错误。"
             "<b>处理</b>：这些日期跳过成交量相关计算。"
             "<b>待办</b>：半日市日历每年从港交所公告更新一次，已列入 SOP 年度事项。"},
]

REPORT_GAPS = [
    "<b>无前瞻性公司行动日历。</b>当前只有已发生的除权除息，拿不到未来的预告。"
    "要补需要接入港交所披露易和 SEC EDGAR 的公告原文并解析。这是下一版最优先项，"
    "因为「明天哪只票除息」比「上个月哪只票除过息」有用得多。",
    "<b>无新股首日检查。</b>次新股页只能从行情起点反推上市时间，"
    "无法区分「新上市」和「数据源覆盖起点晚」，也做不了首日字段完整性检查。",
    "<b>单一数据源。</b>目前全部依赖 yfinance，跨源比对无法进行。"
    "本期发现的问题靠的是数据源内部自洽性检查，这条路能走通但覆盖面有限。"
    "接入 akshare 作为第二源后，可以增加一整个 cross-source 域的规则。",
    "<b>DQ-PRC-002 阈值未分层。</b>异常跳空阈值对全部标的统一取 20%，"
    "在智谱、OKLO、IONQ 这类高波动标的上贡献了大部分命中。"
    "下一步按标的历史波动率分层设定阈值。",
    "<b>台账填写尚未形成习惯。</b>当前所有异常状态都是 open，没有一条人工处理记录。"
    "按 SOP 每日盘后动线补齐，这是整个项目积累价值的地方。",
]


# ----------------------------------------------------------------------

def render_site() -> None:
    con = db.connect()
    env = Environment(
        loader=FileSystemLoader(str(config.TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    meta = build_meta(con)
    # 样式和脚本直接进 <style> / <script>，不能被 HTML 转义
    base = {"meta": meta,
            "hover_css": Markup(charts.HOVER_CSS),
            "hover_js": Markup(charts.HOVER_JS),
            "table_js": Markup(charts.TABLE_JS)}

    site = config.SITE_DIR
    if site.exists():
        shutil.rmtree(site)
    (site / "stock").mkdir(parents=True)
    (site / "ops").mkdir(parents=True)
    shutil.copytree(config.STATIC_DIR, site / "static")

    def write(tpl, out, page, root, ctx):
        (site / out).write_text(
            env.get_template(tpl).render(page=page, root=root, title=ctx.pop("title"),
                                         **base, **ctx),
            encoding="utf-8")

    write("home.html", "index.html", "home", "",
          {"title": "总览", **page_home(con, meta)})
    write("index.html", "watchlist.html", "index", "",
          {"title": "自选股", **page_index(con)})
    write("calendar.html", "calendar.html", "calendar", "",
          {"title": "事件日历", **page_calendar(con)})
    write("newlisting.html", "ops/coverage.html", "newlisting", "../",
          {"title": "数据覆盖", **page_newlisting(con)})

    write("ops_index.html", "ops/index.html", "ops", "../",
          {"title": "异常清单", **page_ops_index(con)})
    write("ops_corpaction.html", "ops/corpaction.html", "corpaction", "../",
          {"title": "复权核对", **page_corpaction(con)})
    write("ops_rules.html", "ops/rules.html", "rules", "../",
          {"title": "规则库", **page_rules(con)})
    write("ops_ledger.html", "ops/ledger.html", "ledger", "../",
          {"title": "处理台账", **page_ledger(con)})
    write("ops_report.html", "ops/report.html", "report", "../",
          {"title": "运营报告", **page_report(con, meta)})

    # 对照标的也生成页面：复权核对页会链接到 BABA、汇丰港股这些，它们正是关键发现的对照组
    symbols = [s for s, in con.execute("SELECT symbol FROM ref_watchlist").fetchall()]
    vols_all = _volatility(con)
    for sym in symbols:
        ctx = page_stock(con, sym, vols_all)
        write("stock.html", f"stock/{slug(sym)}.html", "index", "../",
              {"title": ctx["s"]["name"], **ctx})

    n = len(list(site.rglob("*.html")))
    print(f"站点已生成：{site}")
    print(f"  {n} 个页面（8 个主页面 + {len(symbols)} 个个股页）")
    print(f"  数据截至 {meta['data_date']}，未处理异常 {meta['n_open']} 条，其中 P0 {meta['n_p0']} 条")
    con.close()


if __name__ == "__main__":
    render_site()
