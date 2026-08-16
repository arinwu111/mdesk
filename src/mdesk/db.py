"""建库、建表、灌参考数据。可重复执行。"""

import duckdb

from . import config


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(config.DB_PATH), read_only=read_only)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(config.SCHEMA_SQL.read_text(encoding="utf-8"))


def load_reference(con: duckdb.DuckDBPyConnection) -> None:
    """参考数据以 CSV 为准，每次全量覆盖。改自选股只需要改 CSV。"""
    con.execute("DELETE FROM ref_watchlist")
    con.execute(
        f"""
        INSERT INTO ref_watchlist
        SELECT symbol, display_code, name, market, sector, instrument_type,
               is_case::BOOLEAN, case_note, is_reference::BOOLEAN, CURRENT_DATE
        FROM read_csv_auto('{config.WATCHLIST_CSV}', header=true)
        """
    )

    con.execute("DELETE FROM ref_cross_listing")
    con.execute(
        f"""
        INSERT INTO ref_cross_listing
        SELECT pair_id, symbol_a, symbol_b, ratio, fx_pair, tolerance_pct, note
        FROM read_csv_auto('{config.CROSS_LISTING_CSV}', header=true)
        """
    )


def load_rules(con: duckdb.DuckDBPyConnection) -> None:
    """规则同样以文件为准，但只补不删，避免冲掉手工在库里加的规则。"""
    if config.RULES_SQL.exists():
        con.execute(config.RULES_SQL.read_text(encoding="utf-8"))


def all_symbols(con: duckdb.DuckDBPyConnection) -> list[str]:
    """全部需要采集的代码，含仅作对照的参考标的。"""
    rows = con.execute("SELECT symbol FROM ref_watchlist ORDER BY symbol").fetchall()
    return [r[0] for r in rows]


def fx_pairs(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT fx_pair FROM ref_cross_listing WHERE fx_pair IS NOT NULL AND fx_pair <> ''"
    ).fetchall()
    return [r[0] for r in rows]


def main() -> None:
    con = connect()
    init_schema(con)
    load_reference(con)
    load_rules(con)

    total, cases, refs = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE is_case),
               count(*) FILTER (WHERE is_reference)
        FROM ref_watchlist
        """
    ).fetchone()
    pairs = con.execute("SELECT count(*) FROM ref_cross_listing").fetchone()[0]
    rules = con.execute("SELECT count(*) FROM dq_rules").fetchone()[0]

    print(f"库已就绪: {config.DB_PATH}")
    print(f"  自选股 {total - refs} 只，其中重点案例 {cases} 只")
    print(f"  跨市场对照标的 {refs} 只，对照关系 {pairs} 组")
    print(f"  校验规则 {rules} 条")
    con.close()


if __name__ == "__main__":
    main()
