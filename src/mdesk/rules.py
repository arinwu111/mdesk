"""规则引擎。逐条执行 dq_rules 里的 SQL，把命中写进 dq_results。

两条硬约定：
1. 幂等。同一条命中（规则 + 标的 + 业务日）只有一行，重跑只更新 last_seen 和 detail
2. 人工填的 status / handled_by / handling_note 永远不被覆盖。台账是这个项目最值钱的东西
"""

import hashlib
from datetime import datetime

from . import db


def _result_id(rule_id: str, symbol: str, biz_date) -> str:
    return hashlib.md5(f"{rule_id}|{symbol}|{biz_date}".encode()).hexdigest()[:16]


def run_all(con, verbose: bool = True) -> dict:
    rules = con.execute(
        "SELECT rule_id, name, sql_expr, severity FROM dq_rules WHERE enabled ORDER BY rule_id"
    ).fetchall()

    now = datetime.now()
    stats, broken = {}, []

    for rule_id, name, sql_expr, severity in rules:
        try:
            hits = con.execute(sql_expr).fetchall()
        except Exception as exc:
            # 规则本身写错了也是一种数据质量问题，必须让它显形而不是静默跳过
            broken.append((rule_id, name, f"{type(exc).__name__}: {exc}"))
            stats[rule_id] = -1
            continue

        for row in hits:
            if len(row) != 3:
                broken.append((rule_id, name, f"返回 {len(row)} 列，规则必须返回 3 列"))
                stats[rule_id] = -1
                break
            symbol, biz_date, detail = row
            con.execute(
                """
                INSERT INTO dq_results
                    (result_id, rule_id, symbol, biz_date, severity, detail,
                     status, first_seen, last_seen, handled_by, handled_at, handling_note)
                VALUES (?,?,?,?,?,?,'open',?,?,NULL,NULL,NULL)
                ON CONFLICT (result_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    detail    = excluded.detail,
                    severity  = excluded.severity
                """,
                [_result_id(rule_id, symbol, biz_date), rule_id, symbol, biz_date,
                 severity, str(detail), now, now],
            )
        else:
            stats[rule_id] = len(hits)

    if verbose:
        print(f"规则执行完毕，共 {len(rules)} 条")
        for rule_id, name, _, sev in rules:
            n = stats.get(rule_id, 0)
            mark = "规则报错" if n < 0 else (f"命中 {n}" if n else "无命中")
            print(f"  {rule_id:12} {sev}  {name:22} {mark}")
        if broken:
            print("\n以下规则自身有问题，需要修：")
            for rule_id, name, msg in broken:
                print(f"  {rule_id} {name}: {msg[:120]}")

    return stats


def summary(con) -> None:
    rows = con.execute(
        """
        SELECT severity, status, count(*) AS n
        FROM dq_results GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).fetchall()
    print("\n当前台账")
    for sev, status, n in rows:
        print(f"  {sev}  {status:10} {n:5}")

    print("\n未处理的 P0（SOP 里要求当场处理的那些）")
    top = con.execute(
        """
        SELECT r.rule_id, r.symbol, coalesce(w.name, r.symbol) AS name, r.biz_date, r.detail
        FROM dq_results r
        LEFT JOIN ref_watchlist w ON w.symbol = r.symbol
        WHERE r.severity = 'P0' AND r.status = 'open'
        ORDER BY r.biz_date DESC, r.rule_id
        LIMIT 25
        """
    ).fetchall()
    for rule_id, symbol, name, biz_date, detail in top:
        print(f"  {biz_date} {rule_id:12} {name:14} {detail[:90]}")


def main() -> None:
    con = db.connect()
    db.load_rules(con)
    run_all(con)
    summary(con)
    con.close()


if __name__ == "__main__":
    main()
