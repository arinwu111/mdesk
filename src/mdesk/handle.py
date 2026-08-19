"""填写处理台账。系统负责发现，判断原因和决定怎么修是人做的事。

    python3 -m mdesk.handle DQ-CA-001 9988.HK 2025-06-11 \
        --status explained --note "数据源用美元金额扣减港币价格，复权价以自算为准"

写进去的处理记录用独立字段存放，重跑采集和规则都不会覆盖。
"""

import argparse
import getpass
from datetime import datetime

from . import db, ledger

VALID = ("open", "explained", "fixed", "ignored")


def main() -> None:
    ap = argparse.ArgumentParser(description="填写异常处理记录")
    ap.add_argument("rule_id")
    ap.add_argument("symbol")
    ap.add_argument("biz_date", help="业务日 YYYY-MM-DD")
    ap.add_argument("--status", required=True, choices=VALID)
    ap.add_argument("--note", required=True, help="判断结论，写清楚原因和处理方式")
    ap.add_argument("--by", default=None, help="处理人，默认取当前系统用户")
    args = ap.parse_args()

    con = db.connect()
    row = con.execute(
        "SELECT result_id, detail, status FROM dq_results "
        "WHERE rule_id = ? AND symbol = ? AND biz_date = ?",
        [args.rule_id, args.symbol, args.biz_date],
    ).fetchone()

    if not row:
        print(f"未找到该条异常：{args.rule_id} / {args.symbol} / {args.biz_date}")
        near = con.execute(
            "SELECT rule_id, symbol, biz_date FROM dq_results "
            "WHERE rule_id = ? ORDER BY biz_date DESC LIMIT 5", [args.rule_id]
        ).fetchall()
        if near:
            print("该规则下最近的几条是：")
            for r in near:
                print(f"  {r[0]} {r[1]} {r[2]}")
        con.close()
        return

    result_id, detail, old_status = row
    con.execute(
        "UPDATE dq_results SET status = ?, handled_by = ?, handled_at = ?, "
        "handling_note = ? WHERE result_id = ?",
        [args.status, args.by or getpass.getuser(), datetime.now(), args.note, result_id],
    )
    print(f"已记录　{args.rule_id} / {args.symbol} / {args.biz_date}")
    print(f"  异常内容　{detail}")
    print(f"  状态　　　{old_status} → {args.status}")
    print(f"  处理记录　{args.note}")
    n = ledger.export_ledger(con)
    print(f"\n已同步到 data/ledger.csv（{n} 条），这份文件要提交进仓库，"
          f"否则 CI 重建数据库后判断记录会丢失。")
    print("下次运行 python3 -m mdesk.pipeline --render 后，页面上的角标会随之更新。")
    con.close()


if __name__ == "__main__":
    main()
