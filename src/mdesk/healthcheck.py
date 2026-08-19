"""链路健康检查。退出码非零表示这一轮不可信，用来让 CI 变红而不是静默通过。

定时任务里最危险的失败形态不是报错，是「跑完了、退出码 0、页面照常更新，
但数据其实停在上周」。所以管道跑完必须再问一次：这批数据能用吗。

退出码
  0  健康
  1  数据过期或采集大面积失败，本轮产出不可信
"""

import sys
from datetime import datetime

from . import db

MAX_ERROR_RATIO = 0.25   # 单轮采集失败占比上限


def check(con) -> tuple[bool, list[str]]:
    problems = []

    # 1. 及时性。直接复用 DQ-SYS-001 的判定，不在这里重写一遍逻辑，
    #    否则两处阈值会各自漂移。
    sql = con.execute(
        "SELECT sql_expr FROM dq_rules WHERE rule_id = 'DQ-SYS-001' AND enabled"
    ).fetchone()
    if sql:
        for _sym, _d, detail in con.execute(sql[0]).fetchall():
            problems.append(f"[及时性] {detail}")
    else:
        problems.append("[配置] DQ-SYS-001 不存在或已停用，及时性无人监控")

    # 2. 采集失败占比
    row = con.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'error'), count(*)
        FROM raw_fetch_log
        WHERE run_id = (SELECT max(run_id) FROM raw_fetch_log)
        """
    ).fetchone()
    err, total = row or (0, 0)
    if total and err / total > MAX_ERROR_RATIO:
        problems.append(
            f"[采集] 最近一轮 {total} 个数据集中 {err} 个失败"
            f"（{err / total:.0%}，上限 {MAX_ERROR_RATIO:.0%}）")

    # 3. 空库保护。渲染一个空站点比渲染一个过期站点更糟
    n_price = con.execute("SELECT count(*) FROM price_primary").fetchone()[0]
    if n_price == 0:
        problems.append("[采集] price_primary 为空，本轮没有拿到任何行情")

    return not problems, problems


def main() -> None:
    con = db.connect()
    ok, problems = check(con)
    n_price = con.execute("SELECT count(*) FROM price_primary").fetchone()[0]
    last = con.execute("SELECT max(trade_date) FROM price_primary").fetchone()[0]
    con.close()

    print(f"健康检查 @ {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  行情 {n_price} 行，最新交易日 {last}")
    if ok:
        print("  结论：健康")
        sys.exit(0)
    print(f"  结论：不健康，{len(problems)} 项")
    for p in problems:
        print(f"    {p}")
    sys.exit(1)


if __name__ == "__main__":
    main()
