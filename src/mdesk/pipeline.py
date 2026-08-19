"""一条命令跑完整条链路。

    python3 -m mdesk.pipeline              # 全量：采集 → 前瞻 → 复权 → 规则 → 快照 → 渲染
    python3 -m mdesk.pipeline --render     # 只重渲染，不联网（改了模板或填了台账之后用）
    python3 -m mdesk.pipeline --no-forward # 跳过前瞻事件采集，省掉披露易那段耗时

按 SOP，这条命令由定时任务执行，不需要人工触发。人工的部分是看结果和填台账。
"""

import argparse
import time

from . import (db, fetch, fetch_akshare, forward, ledger, render, report,
               rules, transform)


def run(do_fetch=True, do_forward=True, do_render=True) -> None:
    t0 = time.time()
    con = db.connect()
    db.init_schema(con)
    db.load_reference(con)
    db.load_rules(con)
    con.close()

    if do_fetch:
        print("\n[1/6] 采集主源行情与公司行动　yfinance")
        fetch.main()

        print("\n[2/6] 采集副源行情　akshare")
        try:
            fetch_akshare.main()
        except Exception as exc:
            # 副源挂了不该拖垮整条链路，跨源规则会自己发现覆盖缺口
            print(f"  副源采集异常，跨源校验本轮降级：{type(exc).__name__}: {exc}")

    if do_forward:
        print("\n[3/6] 采集前瞻事件")
        forward.main()

    con = db.connect()
    print("\n[4/6] 重算复权价")
    n = transform.build_adj_price(con)
    print(f"  {n} 行")

    print("\n[5/6] 执行校验规则")
    rules.run_all(con, verbose=True)
    # 规则跑完立刻把人工判断贴回来。顺序不能颠倒：规则会新建命中行，
    # 台账要覆盖在这些新行上，否则页面角标会把已解释的异常重新显示成待查。
    applied, orphan = ledger.apply_ledger(con)
    print(f"  台账贴回 {applied} 条" + (f"，{orphan} 条悬空待确认" if orphan else ""))
    transform.build_snapshot(con)
    # 报告快照必须在台账贴回之后写，否则已关闭数会漏掉本轮贴回的那些
    sid = report.take_snapshot(con)
    print(f"  运营报告快照 {sid} 已写入")
    con.close()

    if do_render:
        print("\n[6/6] 渲染站点")
        render.render_site()

    print(f"\n全链路完成，耗时 {time.time() - t0:.0f} 秒")


def main() -> None:
    ap = argparse.ArgumentParser(description="mdesk 全链路")
    ap.add_argument("--render", action="store_true", help="只重渲染，不联网")
    ap.add_argument("--no-forward", action="store_true", help="跳过前瞻事件采集")
    args = ap.parse_args()

    if args.render:
        con = db.connect()
        db.load_rules(con)
        transform.build_adj_price(con)
        rules.run_all(con, verbose=False)
        transform.build_snapshot(con)
        report.take_snapshot(con)
        con.close()
        render.render_site()
    else:
        run(do_forward=not args.no_forward)


if __name__ == "__main__":
    main()
