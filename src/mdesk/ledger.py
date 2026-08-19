"""处理台账的持久化。

dq_results 里绝大部分字段都能从 raw 层重放出来，但四个字段不能：
status、handled_by、handled_at、handling_note。这四个是人坐下来判断出来的，
一旦丢了没有任何办法自动恢复。

而 data/*.duckdb 不进版本库（二进制、17MB、每次全表重写），
定时任务在 CI 里从零建库，如果不把这四个字段单独存成文本，
每天跑完一次台账就被清空一次。

所以这里把人工字段导出成 CSV 提交进仓库，重建后再贴回去。
机器能重放的数据和人写的数据分开存放，是这个项目的一条硬约定。
"""

import csv

from . import config

LEDGER_CSV = config.DATA_DIR / "ledger.csv"
FIELDS = ["rule_id", "symbol", "biz_date", "status", "handled_by", "handled_at",
          "handling_note"]


def export_ledger(con) -> int:
    """把所有人工处理过的记录导出。status='open' 且无处理记录的不导，那些是机器产生的。"""
    rows = con.execute(
        """
        SELECT rule_id, symbol, biz_date, status, handled_by, handled_at, handling_note
        FROM dq_results
        WHERE handling_note IS NOT NULL OR status <> 'open'
        ORDER BY rule_id, symbol, biz_date
        """
    ).fetchall()
    LEDGER_CSV.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for r in rows:
            w.writerow(["" if v is None else str(v) for v in r])
    return len(rows)


def apply_ledger(con) -> tuple[int, int]:
    """把 CSV 里的人工判断贴回 dq_results。

    返回 (贴回成功数, 找不到对应命中的数)。后者不是错误：
    规则阈值调整后旧命中可能不再出现，那条判断记录就悬空了，
    保留在 CSV 里但贴不上去，需要人工确认是否作废。
    """
    if not LEDGER_CSV.exists():
        return 0, 0
    applied = orphan = 0
    with LEDGER_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n = con.execute(
                """
                UPDATE dq_results SET
                    status = ?, handled_by = ?,
                    handled_at = try_cast(? AS TIMESTAMP), handling_note = ?
                WHERE rule_id = ? AND symbol = ? AND biz_date = try_cast(? AS DATE)
                """,
                [row["status"] or "open", row["handled_by"] or None,
                 row["handled_at"] or None, row["handling_note"] or None,
                 row["rule_id"], row["symbol"], row["biz_date"]],
            ).fetchall()
            # DuckDB 的 UPDATE 不直接返回影响行数，用一次存在性查询确认
            hit = con.execute(
                "SELECT count(*) FROM dq_results WHERE rule_id = ? AND symbol = ? "
                "AND biz_date = try_cast(? AS DATE)",
                [row["rule_id"], row["symbol"], row["biz_date"]],
            ).fetchone()[0]
            if hit:
                applied += 1
            else:
                orphan += 1
    return applied, orphan


def main() -> None:
    from . import db
    con = db.connect()
    n = export_ledger(con)
    print(f"已导出 {n} 条人工处理记录到 {LEDGER_CSV.relative_to(config.ROOT)}")
    con.close()


if __name__ == "__main__":
    main()
