"""第二数据源：akshare。

接第二个源不是为了备份，是为了开一整类新的校验。

单源只能查「内部自洽」——数据源自己跟自己有没有矛盾。阿里那个发现就是这么来的。
但如果一个源给出的值看起来完全合理、内部也自洽，只是数值本身错了，单源永远查不出来。
两个独立的源对同一天同一只票给出不同的收盘价，才能暴露这类问题。

港股走东方财富，美股走新浪。两条链路互相独立，任何一条挂了另一条照常跑。
"""

import time
import warnings
from datetime import datetime, timedelta

import pandas as pd

from . import db

warnings.filterwarnings("ignore")

SOURCE = "akshare"
RETRIES = 3
LOOKBACK_DAYS = 120


def _retry(fn, what: str, tries: int = RETRIES):
    """东财的 push2his 集群偶发代理错误，重试基本能过。失败也要留痕不能吞掉。"""
    last = None
    for i in range(tries):
        try:
            return fn(), None
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.2 * (i + 1))
    return None, f"{what} 重试 {tries} 次仍失败 | {last}"


def _hk_code(symbol: str) -> str:
    """0700.HK → 00700，东财港股用五位代码。"""
    return symbol.split(".")[0].zfill(5)[-5:]


def fetch_hk(symbol: str, start: str, end: str):
    """港股走两条链路。

    首轮实测：东财的 stock_hk_hist 在本机稳定失败，12 只里 8 只报 ProxyError，
    而且三次重试全部撞在同一台 33.push2his.eastmoney.com 上，说明是该主机不可达
    而非瞬时抖动。新浪的 stock_hk_daily 12 只全通且收盘价与 yfinance 一致，
    因此改为新浪优先、东财兜底。
    """
    import akshare as ak
    code = _hk_code(symbol)

    df, err1 = _retry(lambda: ak.stock_hk_daily(symbol=code, adjust=""),
                      f"港股 {symbol} 新浪源", tries=2)
    if df is not None and len(df):
        return df, None

    df, err2 = _retry(
        lambda: ak.stock_hk_hist(symbol=code, period="daily",
                                 start_date=start, end_date=end, adjust=""),
        f"港股 {symbol} 东财源", tries=2)
    if df is not None and len(df):
        return df, None
    return None, f"两条链路均失败 | {err1} | {err2}"


def fetch_us(symbol: str):
    import akshare as ak
    return _retry(lambda: ak.stock_us_daily(symbol=symbol), f"美股 {symbol}")


def _normalize(df, market: str):
    """两个源的列名和类型都不一样，统一成 date/open/high/low/close/volume。"""
    if df is None or len(df) == 0:
        return None
    cn = {"日期": "date", "开盘": "open", "收盘": "close",
          "最高": "high", "最低": "low", "成交量": "volume"}
    df = df.rename(columns={k: v for k, v in cn.items() if k in df.columns})
    need = {"date", "open", "high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        return None
    out = df[list(need)].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    return out


def main() -> None:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-ak"
    con = db.connect()
    wl = con.execute(
        "SELECT symbol, market, name FROM ref_watchlist WHERE NOT is_reference "
        "ORDER BY market, symbol").fetchall()

    end = datetime.now().date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    s_str, e_str = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    print(f"第二数据源采集 run_id={run_id}，{len(wl)} 只标的，回溯 {LOOKBACK_DAYS} 天")
    ok = fail = 0
    for symbol, market, name in wl:
        df, err = (fetch_hk(symbol, s_str, e_str) if market == "HK"
                   else fetch_us(symbol))
        norm = _normalize(df, market)

        if norm is None:
            fail += 1
            msg = err or "返回为空或列名不符，该源可能不覆盖此标的"
            con.execute("INSERT INTO raw_fetch_log VALUES (?,?,?,?,?,?,?,?)",
                        [run_id, symbol, "price", "error" if err else "empty",
                         0, msg[:400], SOURCE, datetime.now()])
            print(f"  ✗ {symbol:10} {name:14} {msg[:64]}")
            time.sleep(0.4)
            continue

        norm = norm[norm["date"] >= start]
        cur = "HKD" if market == "HK" else "USD"
        rows = [
            (symbol, r["date"], float(r["open"]), float(r["high"]), float(r["low"]),
             float(r["close"]), None, int(r["volume"]) if pd.notna(r["volume"]) else None,
             cur, SOURCE, datetime.now())
            for _, r in norm.iterrows() if pd.notna(r["close"])
        ]
        con.execute("DELETE FROM raw_price_daily WHERE symbol = ? AND source = ?",
                    [symbol, SOURCE])
        if rows:
            con.executemany("INSERT INTO raw_price_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT INTO raw_fetch_log VALUES (?,?,?,?,?,?,?,?)",
                    [run_id, symbol, "price", "ok", len(rows), "", SOURCE, datetime.now()])
        ok += 1
        print(f"  ✓ {symbol:10} {name:14} {len(rows):4} 行")
        time.sleep(0.4)

    print(f"\n成功 {ok} 只，失败或未覆盖 {fail} 只")
    cov = con.execute(
        """
        SELECT count(DISTINCT symbol) FILTER (WHERE source = 'yfinance'),
               count(DISTINCT symbol) FILTER (WHERE source = 'akshare')
        FROM raw_price_daily
        """).fetchone()
    print(f"库中覆盖：yfinance {cov[0]} 只，akshare {cov[1]} 只")
    con.close()


if __name__ == "__main__":
    main()
