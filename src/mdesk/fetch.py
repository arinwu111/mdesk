"""采集层。只负责把数据原样搬进 raw，不做任何加工判断。

设计约定：
1. 任何一只标的失败都不能中断整轮，失败本身写进 raw_fetch_log 由校验层去发现
2. 同一 (symbol, source) 的历史整段覆盖，保证可重放
3. 数据源返回的名称也要落库，用来和自选股名单对账
"""

import hashlib
import time
import warnings
from datetime import datetime

import pandas as pd
import yfinance as yf

from . import config, db

warnings.filterwarnings("ignore")

SOURCE = "yfinance"


def _action_id(symbol: str, ex_date, action_type: str) -> str:
    key = f"{symbol}|{ex_date}|{action_type}|{SOURCE}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def _log(con, run_id, symbol, dataset, status, row_count, message=""):
    con.execute(
        "INSERT INTO raw_fetch_log VALUES (?,?,?,?,?,?,?,?)",
        [run_id, symbol, dataset, status, row_count, message[:400], SOURCE, datetime.now()],
    )


def fetch_symbol(con, run_id: str, symbol: str) -> None:
    ticker = yf.Ticker(symbol)

    # ---------- 日线与公司行动 ----------
    try:
        hist = ticker.history(period=f"{config.HISTORY_YEARS}y", auto_adjust=False)
    except Exception as exc:
        _log(con, run_id, symbol, "price", "error", 0, f"{type(exc).__name__}: {exc}")
        return

    if hist is None or hist.empty:
        # 拉不到数据本身就是一条待查线索，交给 DQ-REF-001
        _log(con, run_id, symbol, "price", "empty", 0, "数据源返回空，代码可能无效或已退市")
        return

    hist = hist.reset_index()
    date_col = "Date" if "Date" in hist.columns else hist.columns[0]
    hist[date_col] = pd.to_datetime(hist[date_col]).dt.tz_localize(None).dt.date

    currency = None
    try:
        currency = ticker.fast_info.get("currency")
    except Exception:
        pass

    price_rows = [
        (
            symbol,
            r[date_col],
            _f(r.get("Open")),
            _f(r.get("High")),
            _f(r.get("Low")),
            _f(r.get("Close")),
            _f(r.get("Adj Close")),
            int(r["Volume"]) if pd.notna(r.get("Volume")) else None,
            currency,
            SOURCE,
            datetime.now(),
        )
        for _, r in hist.iterrows()
        if pd.notna(r.get("Close"))
    ]

    con.execute("DELETE FROM raw_price_daily WHERE symbol = ? AND source = ?", [symbol, SOURCE])
    con.executemany(
        "INSERT INTO raw_price_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)", price_rows
    )
    _log(con, run_id, symbol, "price", "ok", len(price_rows))

    # ---------- 公司行动 ----------
    actions = []
    for _, r in hist.iterrows():
        d = r[date_col]
        div = _f(r.get("Dividends")) or 0.0
        spl = _f(r.get("Stock Splits")) or 0.0
        if div > 0:
            actions.append(
                (_action_id(symbol, d, "cash_dividend"), symbol, d, "cash_dividend",
                 None, div, currency, SOURCE, datetime.now(), "")
            )
        if spl and spl != 1.0:
            # yfinance 约定：10 拆 1 记为 10.0，1 合 10 记为 0.1
            kind = "split" if spl > 1 else "reverse_split"
            actions.append(
                (_action_id(symbol, d, kind), symbol, d, kind,
                 spl, None, currency, SOURCE, datetime.now(),
                 f"数据源上报比例 {spl}")
            )

    con.execute(
        "DELETE FROM events_corporate_action WHERE symbol = ? AND source = ?", [symbol, SOURCE]
    )
    if actions:
        con.executemany(
            "INSERT INTO events_corporate_action VALUES (?,?,?,?,?,?,?,?,?,?)", actions
        )
    _log(con, run_id, symbol, "corp_action", "ok" if actions else "empty", len(actions))

    # ---------- 基本面快照 ----------
    try:
        info = ticker.info or {}
    except Exception as exc:
        _log(con, run_id, symbol, "fundamental", "error", 0, f"{type(exc).__name__}: {exc}")
        return

    today = datetime.now().date()
    con.execute(
        "DELETE FROM raw_fundamental WHERE symbol = ? AND snapshot_date = ? AND source = ?",
        [symbol, today, SOURCE],
    )
    con.execute(
        "INSERT INTO raw_fundamental VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            symbol, today,
            _f(info.get("marketCap")),
            _f(info.get("sharesOutstanding")),
            _f(info.get("trailingEps")),
            _f(info.get("trailingPE")),
            _f(info.get("priceToBook")),
            _f(info.get("priceToSalesTrailing12Months")),
            _f(info.get("dividendYield")),
            info.get("currency") or currency,
            info.get("longName") or info.get("shortName"),
            info.get("exchange"),
            info.get("quoteType"),
            SOURCE, datetime.now(),
        ],
    )
    _log(con, run_id, symbol, "fundamental", "ok", 1)


def fetch_fx(con, run_id: str, pair: str) -> None:
    try:
        hist = yf.Ticker(pair).history(period=f"{config.HISTORY_YEARS}y")
    except Exception as exc:
        _log(con, run_id, pair, "fx", "error", 0, f"{type(exc).__name__}: {exc}")
        return
    if hist is None or hist.empty:
        _log(con, run_id, pair, "fx", "empty", 0, "汇率数据为空")
        return

    hist = hist.reset_index()
    date_col = "Date" if "Date" in hist.columns else hist.columns[0]
    hist[date_col] = pd.to_datetime(hist[date_col]).dt.tz_localize(None).dt.date
    rows = [
        (pair, r[date_col], _f(r["Close"]), SOURCE, datetime.now())
        for _, r in hist.iterrows()
        if pd.notna(r.get("Close"))
    ]
    con.execute("DELETE FROM raw_fx_daily WHERE pair = ? AND source = ?", [pair, SOURCE])
    con.executemany("INSERT INTO raw_fx_daily VALUES (?,?,?,?,?)", rows)
    _log(con, run_id, pair, "fx", "ok", len(rows))


def _f(v):
    """统一转 float，None / NaN / 空串一律返回 None。"""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> None:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    con = db.connect()
    symbols = db.all_symbols(con)
    pairs = db.fx_pairs(con)

    print(f"采集开始 run_id={run_id}，标的 {len(symbols)} 只，汇率 {len(pairs)} 组")
    for i, sym in enumerate(symbols, 1):
        try:
            fetch_symbol(con, run_id, sym)
        except Exception as exc:  # 单只失败不影响整轮
            _log(con, run_id, sym, "unknown", "error", 0, f"{type(exc).__name__}: {exc}")
        print(f"  [{i}/{len(symbols)}] {sym}", flush=True)
        time.sleep(0.4)

    for pair in pairs:
        fetch_fx(con, run_id, pair)
        time.sleep(0.4)

    summary = con.execute(
        """
        SELECT dataset, status, count(*) AS n, coalesce(sum(row_count), 0) AS rows
        FROM raw_fetch_log WHERE run_id = ?
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        [run_id],
    ).fetchall()
    print("\n本轮采集结果")
    for dataset, status, n, rows in summary:
        print(f"  {dataset:12} {status:6} {n:3} 个标的  {int(rows):>7} 行")

    failures = con.execute(
        "SELECT symbol, dataset, status, message FROM raw_fetch_log "
        "WHERE run_id = ? AND status <> 'ok' ORDER BY symbol",
        [run_id],
    ).fetchall()
    if failures:
        print("\n非成功记录（交给校验层处理，不在这里下判断）")
        for sym, ds, st, msg in failures:
            print(f"  {sym:12} {ds:12} {st:6} {msg[:70]}")

    con.close()


if __name__ == "__main__":
    main()
