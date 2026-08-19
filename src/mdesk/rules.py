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
    executed = []          # 本轮成功执行过的规则，只有这些才允许自动关闭旧命中

    for rule_id, name, sql_expr, severity in rules:
        try:
            hits = con.execute(sql_expr).fetchall()
        except Exception as exc:
            # 规则本身写错了也是一种数据质量问题，必须让它显形而不是静默跳过
            broken.append((rule_id, name, f"{type(exc).__name__}: {exc}"))
            stats[rule_id] = -1
            continue
        executed.append(rule_id)

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

    # 条件已消失的旧命中自动关闭。
    #
    # 起因：DQ-SYS-001 在数据过期时命中 2 条，管道补齐数据后规则不再命中，
    # 但那 2 行仍是 open，总览页顶部的过期红条继续显示，
    # 页面写着「数据不可信」而数据其实已经是新的。
    # 时效性这类瞬时规则，条件消失就等于问题已解决，必须自己关掉。
    #
    # 两条保护：
    # 一、只对本轮成功执行过的规则生效。规则报错时返回空结果，
    #     若不加这个限制，一条写错的 SQL 会把它名下所有历史命中一次性关掉。
    # 二、只动 open，不碰人工填过的 explained / fixed / ignored，台账永远优先于机器。
    resolved = 0
    if executed:
        ph = ",".join("?" * len(executed))
        resolved = con.execute(
            f"SELECT count(*) FROM dq_results WHERE status = 'open' "
            f"AND rule_id IN ({ph}) AND last_seen < ?", executed + [now]).fetchone()[0]
        if resolved:
            con.execute(
                f"UPDATE dq_results SET status = 'resolved', handled_at = ?, "
                f"handling_note = coalesce(handling_note, "
                f"'规则条件已消失，本轮未再命中，系统自动关闭') "
                f"WHERE status = 'open' AND rule_id IN ({ph}) AND last_seen < ?",
                [now] + executed + [now])

    if verbose:
        if resolved:
            print(f"  {resolved} 条旧命中的条件已消失，已自动关闭")
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
