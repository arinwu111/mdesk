"""运营报告的跨期度量。

四个指标：本期命中数（与上期对比）、已关闭 / 未关闭、平均关闭时长、按规则的误报率。

关于误报率的口径，这里和直觉定义有一处刻意的偏离，必须写清楚：

  status 有四个取值，其中三个代表「已判定」，含义各不相同：
    explained  规则命中正确，问题真实存在，根因已查明，数据改用替代口径
    fixed      规则命中正确，已直接修正数据
    ignored    规则命中了，但经查这不是问题

  只有 ignored 才是误报。explained 不是。
  阿里那笔派息复权错误就是 explained —— 规则报对了，数据真的错了，
  只是查清了根因并改用自算复权价。把它算进误报率，会把这个项目最有价值的
  一次真实发现标记成规则的失误，方向正好反了。

  所以：误报率 = ignored / 已判定数，分母不含 open。
  未判定的命中不能进分母，否则「还没来得及看」会被算成「规则准」。
"""

import hashlib
from datetime import datetime

from . import db

MIN_SAMPLE = 5      # 已判定数低于这个值不给误报率，样本太少算出来没有意义


def _overall(con) -> dict:
    r = con.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE status = 'open'),
               count(*) FILTER (WHERE status = 'explained'),
               count(*) FILTER (WHERE status = 'fixed'),
               count(*) FILTER (WHERE status = 'ignored'),
               avg(date_diff('minute', first_seen, handled_at) / 60.0)
                 FILTER (WHERE handled_at IS NOT NULL AND first_seen IS NOT NULL)
        FROM dq_results
        """
    ).fetchone()
    n, n_open, n_ex, n_fix, n_ig, avg_hrs = r
    return {"n_hits": n, "n_open": n_open, "n_explained": n_ex, "n_fixed": n_fix,
            "n_ignored": n_ig, "n_closed": n_ex + n_fix + n_ig,
            "avg_close_hrs": avg_hrs}


def take_snapshot(con) -> str:
    """写入当日快照。一天一行，同日重跑覆盖。"""
    o = _overall(con)
    sid = datetime.now().strftime("%Y-%m-%d")
    con.execute("DELETE FROM dq_report_snapshot WHERE snapshot_id = ?", [sid])
    con.execute(
        "INSERT INTO dq_report_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [sid, datetime.now(),
         con.execute("SELECT max(trade_date) FROM price_primary").fetchone()[0],
         con.execute("SELECT count(*) FROM dq_rules WHERE enabled").fetchone()[0],
         o["n_hits"], o["n_open"], o["n_closed"], o["n_explained"], o["n_fixed"],
         o["n_ignored"], o["avg_close_hrs"],
         con.execute("SELECT count(*) FROM price_primary").fetchone()[0]],
    )
    return sid


def metrics(con) -> dict:
    """报告页要用的全部跨期指标。样本不足的一律返回 None 并附原因。"""
    o = _overall(con)
    sid = datetime.now().strftime("%Y-%m-%d")

    prev = con.execute(
        "SELECT snapshot_id, n_hits, n_open, n_closed FROM dq_report_snapshot "
        "WHERE snapshot_id < ? ORDER BY snapshot_id DESC LIMIT 1", [sid]).fetchone()
    history = con.execute(
        "SELECT snapshot_id, n_hits, n_open, n_closed, n_rules "
        "FROM dq_report_snapshot ORDER BY snapshot_id").fetchall()

    # 按规则的误报率
    by_rule = []
    for rid, name, sev, n, n_open, n_ex, n_fix, n_ig in con.execute(
        """
        SELECT r.rule_id, u.name, r.severity, count(*),
               count(*) FILTER (WHERE r.status = 'open'),
               count(*) FILTER (WHERE r.status = 'explained'),
               count(*) FILTER (WHERE r.status = 'fixed'),
               count(*) FILTER (WHERE r.status = 'ignored')
        FROM dq_results r LEFT JOIN dq_rules u ON u.rule_id = r.rule_id
        GROUP BY 1, 2, 3 ORDER BY count(*) DESC
        """
    ).fetchall():
        judged = n_ex + n_fix + n_ig
        by_rule.append({
            "rule_id": rid, "name": name or "", "severity": sev,
            "n": n, "n_open": n_open, "judged": judged,
            "n_explained": n_ex, "n_fixed": n_fix, "n_ignored": n_ig,
            "fp_rate": (n_ig / judged * 100) if judged >= MIN_SAMPLE else None,
            "fp_note": ("" if judged >= MIN_SAMPLE
                        else f"已判定 {judged} 条，不足 {MIN_SAMPLE} 条，不给比率"),
        })

    return {
        "o": o,
        "prev": ({"snapshot_id": prev[0], "n_hits": prev[1], "n_open": prev[2],
                  "n_closed": prev[3],
                  "d_hits": o["n_hits"] - prev[1],
                  "d_open": o["n_open"] - prev[2],
                  "d_closed": o["n_closed"] - prev[3]} if prev else None),
        "history": [{"snapshot_id": h[0], "n_hits": h[1], "n_open": h[2],
                     "n_closed": h[3], "n_rules": h[4]} for h in history],
        "by_rule": by_rule,
        "min_sample": MIN_SAMPLE,
    }


def main() -> None:
    con = db.connect()
    sid = take_snapshot(con)
    m = metrics(con)
    o = m["o"]
    print(f"快照 {sid} 已写入")
    print(f"  命中 {o['n_hits']}　未关闭 {o['n_open']}　已关闭 {o['n_closed']}"
          f"（explained {o['n_explained']} / fixed {o['n_fixed']} / ignored {o['n_ignored']}）")
    print(f"  平均关闭时长 "
          + (f"{o['avg_close_hrs']:.1f} 小时" if o["avg_close_hrs"] is not None else "无数据"))
    print(f"  上期对比: " + (f"{m['prev']['snapshot_id']}" if m["prev"] else "无上期快照，本次是第一期"))
    print()
    print("  按规则的误报率（误报 = ignored，不含 explained）:")
    for r in m["by_rule"]:
        rate = f"{r['fp_rate']:.0f}%" if r["fp_rate"] is not None else "不可计算"
        print(f"    {r['rule_id']:12} 命中{r['n']:3} 已判定{r['judged']:3} "
              f"误报率 {rate:9} {r['fp_note']}")
    con.close()


if __name__ == "__main__":
    main()
