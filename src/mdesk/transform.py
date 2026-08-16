"""结论层。核心是自己算一遍复权价，然后和数据源给的对账。

复权口径（后复权基准取最新交易日，与 yfinance 的 Adj Close 一致，才可比）：
  最新一日因子为 1，从后往前逐日累乘
  遇到拆股比例 R（10 拆 1 记 10.0）  → 更早的日期因子乘 1/R
  遇到每股现金派息 D                  → 更早的日期因子乘 (前收 - D) / 前收
"""

from datetime import datetime

import pandas as pd

from . import db


def build_adj_price(con) -> int:
    prices = con.execute(
        """
        SELECT symbol, trade_date, close, adj_close
        FROM raw_price_daily
        WHERE close IS NOT NULL AND close > 0
        ORDER BY symbol, trade_date
        """
    ).df()

    actions = con.execute(
        """
        SELECT symbol, ex_date, action_type, ratio, amount
        FROM events_corporate_action
        ORDER BY symbol, ex_date
        """
    ).df()

    act_by_symbol: dict[str, dict] = {}
    for _, a in actions.iterrows():
        act_by_symbol.setdefault(a["symbol"], {}).setdefault(a["ex_date"], []).append(a)

    out = []
    for symbol, grp in prices.groupby("symbol", sort=False):
        grp = grp.sort_values("trade_date").reset_index(drop=True)
        events = act_by_symbol.get(symbol, {})
        n = len(grp)
        factors = [1.0] * n

        # 从最新一日往回推
        for i in range(n - 2, -1, -1):
            f = factors[i + 1]
            ex_date = grp.at[i + 1, "trade_date"]
            prev_close = grp.at[i, "close"]
            for a in events.get(ex_date, []):
                if a["action_type"] in ("split", "reverse_split") and a["ratio"]:
                    f *= 1.0 / float(a["ratio"])
                elif a["action_type"] == "cash_dividend" and a["amount"] and prev_close > 0:
                    f *= max(prev_close - float(a["amount"]), 1e-9) / prev_close
            factors[i] = f

        for i in range(n):
            close = float(grp.at[i, "close"])
            src = grp.at[i, "adj_close"]
            src = float(src) if pd.notna(src) else None
            self_adj = close * factors[i]
            diff = (self_adj / src - 1.0) * 100.0 if src else None
            out.append(
                (symbol, grp.at[i, "trade_date"], close, factors[i], self_adj, src, diff)
            )

    con.execute("DELETE FROM mart_adj_price")
    con.executemany("INSERT INTO mart_adj_price VALUES (?,?,?,?,?,?,?)", out)
    return len(out)


def build_snapshot(con) -> int:
    """自选股当日快照。角标状态由 dq_results 决定，所以必须在规则跑完之后调用。"""
    con.execute("DELETE FROM mart_watchlist_snapshot")
    con.execute(
        """
        INSERT INTO mart_watchlist_snapshot
        WITH px AS (
            SELECT symbol, trade_date, close, volume, currency,
                   lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close
            FROM raw_price_daily
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) = 1
        ),
        fun AS (
            SELECT symbol, market_cap, pe_ttm, pb
            FROM raw_fundamental
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY snapshot_date DESC) = 1
        ),
        dq AS (
            SELECT symbol,
                   max(CASE WHEN status = 'open' THEN 2
                            WHEN status = 'explained' THEN 1 ELSE 0 END) AS lvl,
                   string_agg(rule_id || ' ' || detail, ' ｜ '
                              ORDER BY severity, rule_id) AS summary
            FROM dq_results
            WHERE status IN ('open', 'explained')
            GROUP BY symbol
        )
        SELECT w.symbol, w.display_code, w.name, w.market, w.sector,
               px.trade_date, px.close, px.prev_close,
               CASE WHEN px.prev_close > 0
                    THEN (px.close / px.prev_close - 1) * 100 END,
               px.volume, px.currency,
               fun.market_cap, fun.pe_ttm, fun.pb,
               CASE coalesce(dq.lvl, 0) WHEN 2 THEN 'open'
                                        WHEN 1 THEN 'explained'
                                        ELSE 'ok' END,
               coalesce(dq.summary, '今日无异常')
        FROM ref_watchlist w
        LEFT JOIN px  ON px.symbol  = w.symbol
        LEFT JOIN fun ON fun.symbol = w.symbol
        LEFT JOIN dq  ON dq.symbol  = w.symbol
        WHERE NOT w.is_reference
        """
    )
    return con.execute("SELECT count(*) FROM mart_watchlist_snapshot").fetchone()[0]


def main() -> None:
    con = db.connect()
    n = build_adj_price(con)
    print(f"复权价已重算 {n} 行 @ {datetime.now():%H:%M:%S}")

    bad = con.execute(
        """
        SELECT symbol, count(*) AS n, round(max(abs(diff_pct)), 3) AS max_diff
        FROM mart_adj_price
        WHERE src_adj IS NOT NULL AND abs(diff_pct) > 0.5
        GROUP BY symbol ORDER BY max_diff DESC
        """
    ).fetchall()
    if bad:
        print("自算复权价与数据源存在偏离的标的：")
        for sym, n_rows, max_diff in bad:
            print(f"  {sym:12} {n_rows:4} 行偏离，最大 {max_diff}%")
    else:
        print("自算复权价与数据源全部一致")
    con.close()


if __name__ == "__main__":
    main()
