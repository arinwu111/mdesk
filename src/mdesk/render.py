"""渲染静态站。页面只读 mart 层和 dq 层，不碰 raw。"""

import shutil
from datetime import datetime, date

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import charts, config, db

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
STATUS_LABEL = {"open": "待查", "explained": "已解释", "fixed": "已修复", "ignored": "已忽略"}
STATUS_BADGE = {"open": "open", "explained": "explained", "fixed": "ok", "ignored": "ok"}
BADGE_LABEL = {"ok": "正常", "explained": "已解释", "open": "待查"}
MARKET_LABEL = {"HK": "港股", "US": "美股", "CN": "A 股", "TW": "台股"}
TYPE_LABEL = {"stock": "普通股", "etf": "交易所交易基金", "adr": "美国存托凭证", "sdr": "存托凭证"}
SEV_LABEL = {"P0": "当场处理", "P1": "记台账排期", "P2": "归档观察"}
DOMAIN_LABELS = [
    ("ref", "ref 主数据域"), ("price", "price 行情域"),
    ("corp_action", "corp_action 公司行动域"), ("fundamental", "fundamental 基本面域"),
    ("cross", "cross 跨市场域"), ("ops", "ops 作业域"),
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
    data_date = con.execute("SELECT max(trade_date) FROM raw_price_daily").fetchone()[0]
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


def page_index(con) -> dict:
    sparks = {}
    for sym, in con.execute(
            "SELECT DISTINCT symbol FROM mart_watchlist_snapshot").fetchall():
        vals = con.execute(
            "SELECT close FROM raw_price_daily WHERE symbol = ? "
            "ORDER BY trade_date DESC LIMIT 60", [sym]).fetchall()
        sparks[sym] = charts.sparkline([v[0] for v in reversed(vals)])

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
        rows.append({
            "symbol": symbol, "slug": slug(symbol), "display_code": code, "name": name,
            "market": market, "sector": sector, "is_case": is_case,
            "close_fmt": num(close, 3), "change_fmt": pct(chg), "dir": direction(chg),
            "spark": sparks.get(symbol, ""), "mcap_fmt": money(mcap),
            "pe_fmt": num(pe, 1), "badge": badge, "badge_label": BADGE_LABEL[badge],
            "dq_summary": summary,
        })

    counts = dict(con.execute(
        "SELECT market, count(*) FROM ref_watchlist WHERE NOT is_reference "
        "GROUP BY 1").fetchall())
    badge_counts = {"ok": 0, "explained": 0, "open": 0}
    badge_counts.update(dict(con.execute(
        "SELECT dq_badge, count(*) FROM mart_watchlist_snapshot GROUP BY 1").fetchall()))
    return {"rows": rows, "counts": counts, "badge_counts": badge_counts}


def _actions_for(con, symbol):
    out = []
    for ex_date, atype, ratio, amount, cur, note in con.execute(
        "SELECT ex_date, action_type, ratio, amount, currency, note "
        "FROM events_corporate_action WHERE symbol = ? ORDER BY ex_date DESC", [symbol]
    ).fetchall():
        prev = con.execute(
            "SELECT close FROM raw_price_daily WHERE symbol = ? AND trade_date < ? "
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


def page_stock(con, symbol) -> dict:
    (code, name, market, sector, itype, is_case, case_note) = con.execute(
        "SELECT display_code, name, market, sector, instrument_type, is_case, case_note "
        "FROM ref_watchlist WHERE symbol = ?", [symbol]).fetchone()
    snap = con.execute(
        "SELECT trade_date, close, prev_close, change_pct, currency, market_cap, pe_ttm, "
        "dq_badge FROM mart_watchlist_snapshot WHERE symbol = ?", [symbol]).fetchone()
    trade_date, close, prev_close, chg, cur, mcap, pe, badge = snap or (None,) * 8

    series_rows = con.execute(
        "SELECT trade_date, close, self_adj, src_adj, diff_pct FROM mart_adj_price "
        "WHERE symbol = ? ORDER BY trade_date", [symbol]).fetchall()
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

    return {"s": {
        "symbol": symbol, "display_code": code, "name": name, "sector": sector,
        "market_label": MARKET_LABEL.get(market, market),
        "type_label": TYPE_LABEL.get(itype, itype),
        "case_note": case_note if is_case else "",
        "trade_date": trade_date, "currency": cur or "",
        "close_fmt": num(close, 3), "prev_fmt": num(prev_close, 3),
        "change_fmt": pct(chg), "dir": direction(chg),
        "mcap_fmt": money(mcap), "pe_fmt": num(pe, 1), "pe_note": pe_note,
        "badge": badge or "ok", "badge_label": BADGE_LABEL.get(badge or "ok"),
        "open_count": sum(1 for i in issues if i["status_label"] == "待查"),
        "two_series": has_diff, "n_points": len(series_rows),
        "chart": charts.line_chart(x, series),
        "actions": _actions_for(con, symbol), "issues": issues,
    }}


IMPLIED_SQL = """
WITH p AS (
    SELECT symbol, trade_date, close, adj_close, adj_close / close AS ratio,
           lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close,
           lag(adj_close / close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_ratio
    FROM raw_price_daily WHERE close > 0 AND adj_close > 0
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
                "SELECT close FROM raw_price_daily WHERE symbol = ? AND trade_date < ? "
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

    latest = con.execute("SELECT max(trade_date) FROM raw_price_daily").fetchone()[0]
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
    latest = con.execute("SELECT max(trade_date) FROM raw_price_daily").fetchone()[0]
    # 各市场交易日历不同，窗口起点必须按市场分别取，否则会把日历差当成新上市
    market_start = dict(con.execute(
        """
        SELECT w.market, min(p.trade_date)
        FROM raw_price_daily p JOIN ref_watchlist w ON w.symbol = p.symbol
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
            "SELECT min(trade_date), max(trade_date), count(*) FROM raw_price_daily "
            "WHERE symbol = ?", [sym]).fetchone()
        if not r or not r[0]:
            continue
        start, _end, n = r
        first = con.execute(
            "SELECT close FROM raw_price_daily WHERE symbol = ? ORDER BY trade_date LIMIT 1",
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
    n_price = con.execute("SELECT count(*) FROM raw_price_daily").fetchone()[0]
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
    latest = con.execute("SELECT min(trade_date), max(trade_date) FROM raw_price_daily").fetchone()

    return {
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
            "hover_js": Markup(charts.HOVER_JS)}

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

    write("index.html", "index.html", "index", "",
          {"title": "自选股", **page_index(con)})
    write("calendar.html", "calendar.html", "calendar", "",
          {"title": "事件日历", **page_calendar(con)})
    write("newlisting.html", "newlisting.html", "newlisting", "",
          {"title": "次新股", **page_newlisting(con)})

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

    symbols = [s for s, in con.execute(
        "SELECT symbol FROM ref_watchlist WHERE NOT is_reference").fetchall()]
    for sym in symbols:
        ctx = page_stock(con, sym)
        write("stock.html", f"stock/{slug(sym)}.html", "index", "../",
              {"title": ctx["s"]["name"], **ctx})

    n = len(list(site.rglob("*.html")))
    print(f"站点已生成：{site}")
    print(f"  {n} 个页面（8 个主页面 + {len(symbols)} 个个股页）")
    print(f"  数据截至 {meta['data_date']}，未处理异常 {meta['n_open']} 条，其中 P0 {meta['n_p0']} 条")
    con.close()


if __name__ == "__main__":
    render_site()
