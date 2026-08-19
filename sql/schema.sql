-- mdesk 数据模型
-- 分五层：ref 参考 / raw 采集 / events 事件 / dq 校验 / mart 结论
-- 原则：raw 层只落原样数据不加工，所有加工结果可从 raw 重放

------------------------------------------------------------
-- ref 参考层：人工维护的主数据
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ref_watchlist (
    symbol           VARCHAR PRIMARY KEY,  -- 数据源使用的代码，如 0700.HK / NVDA
    display_code     VARCHAR,              -- 人看的代码，如 HK 00700 / US NVDA
    name             VARCHAR,
    market           VARCHAR,              -- HK / US / CN / TW
    sector           VARCHAR,
    instrument_type  VARCHAR,              -- stock / etf / adr / sdr
    is_case          BOOLEAN,              -- 是否列入 20 只重点案例池
    case_note        VARCHAR,              -- 该标的的数据案例价值
    is_reference     BOOLEAN,              -- true = 仅作跨市场对照拉取，不在自选股页展示
    added_at         DATE
);

-- 跨市场对照关系：同一家公司在两地上市，价格应可通过比例和汇率互相折算
CREATE TABLE IF NOT EXISTS ref_cross_listing (
    pair_id      VARCHAR PRIMARY KEY,
    symbol_a     VARCHAR,   -- 主标的（在自选股里的那个）
    symbol_b     VARCHAR,   -- 对照标的
    ratio        DOUBLE,    -- 1 股 B 相当于 ratio 股 A
    fx_pair      VARCHAR,   -- 折算汇率，如 HKDUSD=X；同币种留空
    tolerance_pct DOUBLE,   -- 允许的价差百分比，超过即告警
    note         VARCHAR
);

------------------------------------------------------------
-- raw 采集层：原样落盘，带来源和抓取时间，可追溯
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS raw_price_daily (
    symbol      VARCHAR,
    trade_date  DATE,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    adj_close   DOUBLE,     -- 数据源自己给的复权价，用来和我们自算的对账
    volume      BIGINT,
    currency    VARCHAR,
    source      VARCHAR,
    fetched_at  TIMESTAMP,
    PRIMARY KEY (symbol, trade_date, source)
);

CREATE TABLE IF NOT EXISTS raw_fundamental (
    symbol             VARCHAR,
    snapshot_date      DATE,
    market_cap         DOUBLE,
    shares_outstanding DOUBLE,
    eps_ttm            DOUBLE,
    pe_ttm             DOUBLE,
    pb                 DOUBLE,
    ps_ttm             DOUBLE,
    dividend_yield     DOUBLE,
    currency           VARCHAR,
    source_name        VARCHAR,   -- 数据源返回的证券名称，用来和 ref_watchlist 对账
    exchange           VARCHAR,
    quote_type         VARCHAR,   -- EQUITY / ETF / MUTUALFUND，决定哪些字段本就不适用
    source             VARCHAR,
    fetched_at         TIMESTAMP,
    PRIMARY KEY (symbol, snapshot_date, source)
);

CREATE TABLE IF NOT EXISTS raw_fx_daily (
    pair        VARCHAR,
    trade_date  DATE,
    rate        DOUBLE,
    source      VARCHAR,
    fetched_at  TIMESTAMP,
    PRIMARY KEY (pair, trade_date, source)
);

-- 采集作业日志：哪次跑了什么、成功没成功。异常排查的第一现场
CREATE TABLE IF NOT EXISTS raw_fetch_log (
    run_id      VARCHAR,
    symbol      VARCHAR,
    dataset     VARCHAR,    -- price / fundamental / corp_action / fx
    status      VARCHAR,    -- ok / empty / error
    row_count   BIGINT,
    message     VARCHAR,
    source      VARCHAR,
    fetched_at  TIMESTAMP
);

-- 主源视图。接入第二数据源之后，raw_price_daily 里同一 (symbol, trade_date)
-- 会有多行。任何按 symbol 分区的窗口函数若不限定来源，lag() 取到的「前一日」
-- 其实是同一天另一个源的价格，涨跌幅会被算成接近零，真实跳空被静默吃掉。
-- 所有分析类查询一律走这个视图，只有跨源比对规则才直接读 raw_price_daily。
CREATE OR REPLACE VIEW price_primary AS
SELECT * FROM raw_price_daily WHERE source = 'yfinance';

------------------------------------------------------------
-- events 事件层：整个系统的枢纽
-- 对运营视图它是"异常的解释"，对日常视图它是"我要知道的事"
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events_corporate_action (
    action_id    VARCHAR PRIMARY KEY,  -- md5(symbol|ex_date|action_type|source)
    symbol       VARCHAR,
    ex_date      DATE,
    action_type  VARCHAR,   -- cash_dividend / split / reverse_split / spinoff
                            -- / rights / bonus / scrip_dividend / ratio_change
    ratio        DOUBLE,    -- 拆合股比例：10 拆 1 记 10.0，5 合 1 记 0.2
    amount       DOUBLE,    -- 每股现金金额
    currency     VARCHAR,
    source       VARCHAR,
    fetched_at   TIMESTAMP,
    note         VARCHAR
);

-- 前瞻事件。与 events_corporate_action 的区别是这里放「还没发生」的
CREATE TABLE IF NOT EXISTS events_calendar (
    event_id    VARCHAR PRIMARY KEY,
    symbol      VARCHAR,
    event_date  DATE,      -- 事件预计发生日
    event_type  VARCHAR,   -- earnings / ex_dividend / split / announcement
                           -- / rights / halt / other
    title       VARCHAR,
    url         VARCHAR,
    source      VARCHAR,
    fetched_at  TIMESTAMP
);

-- 结构化补充字段（派息金额、登记日、派付日等），JSON 存放，不同源字段不一
ALTER TABLE events_calendar ADD COLUMN IF NOT EXISTS payload VARCHAR;
ALTER TABLE events_calendar ADD COLUMN IF NOT EXISTS is_past BOOLEAN;

------------------------------------------------------------
-- dq 校验层：规则是数据不是代码，加规则 = 插一行
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dq_rules (
    rule_id     VARCHAR PRIMARY KEY,  -- DQ-<域>-<序号>，如 DQ-REF-001
    domain      VARCHAR,   -- ref / price / corp_action / fundamental / cross
    name        VARCHAR,
    description VARCHAR,   -- 这条规则在查什么，给人看的
    sql_expr    VARCHAR,   -- 必须返回 (symbol, biz_date, detail) 三列
    severity    VARCHAR,   -- P0 / P1 / P2
    enabled     BOOLEAN,
    created_at  DATE,
    note        VARCHAR    -- 为什么加这条规则，来自哪次事故。别省，这是项目的演进史
);

-- 质量维度。domain 回答「查哪块数据」，dimension 回答「查的是哪种质量问题」，
-- 两者正交：同一个 price 域里既有准确性规则也有及时性规则。
-- 取值：完整性 / 准确性 / 一致性 / 及时性 / 合规性
ALTER TABLE dq_rules ADD COLUMN IF NOT EXISTS dimension VARCHAR;

-- 命中记录。重跑幂等：同一条命中只更新 last_seen，人工填的处理意见永不被覆盖
CREATE TABLE IF NOT EXISTS dq_results (
    result_id     VARCHAR PRIMARY KEY,  -- md5(rule_id|symbol|biz_date)
    rule_id       VARCHAR,
    symbol        VARCHAR,
    biz_date      DATE,
    severity      VARCHAR,
    detail        VARCHAR,
    status        VARCHAR,   -- open 待查 / explained 已解释 / fixed 已修复 / ignored 忽略
    first_seen    TIMESTAMP,
    last_seen     TIMESTAMP,
    handled_by    VARCHAR,
    handled_at    TIMESTAMP,
    handling_note VARCHAR    -- 人工判断记录，项目最值钱的部分
);

------------------------------------------------------------
-- mart 结论层：页面只读这层
------------------------------------------------------------

-- 自算复权因子，与数据源给的 adj_close 对账
CREATE TABLE IF NOT EXISTS mart_adj_price (
    symbol        VARCHAR,
    trade_date    DATE,
    close         DOUBLE,
    self_factor   DOUBLE,   -- 自算累计复权因子
    self_adj      DOUBLE,   -- 自算复权价
    src_adj       DOUBLE,   -- 数据源复权价
    diff_pct      DOUBLE,   -- 两者偏离，超阈值即命中 DQ-CA 规则
    PRIMARY KEY (symbol, trade_date)
);

-- 自选股当日快照，日常视图首页直接读它
CREATE TABLE IF NOT EXISTS mart_watchlist_snapshot (
    symbol        VARCHAR PRIMARY KEY,
    display_code  VARCHAR,
    name          VARCHAR,
    market        VARCHAR,
    sector        VARCHAR,
    trade_date    DATE,
    close         DOUBLE,
    prev_close    DOUBLE,
    change_pct    DOUBLE,
    volume        BIGINT,
    currency      VARCHAR,
    market_cap    DOUBLE,
    pe_ttm        DOUBLE,
    pb            DOUBLE,
    dq_badge      VARCHAR,   -- ok 灰 / explained 黄 / open 红
    dq_summary    VARCHAR    -- 角标点开看到的话，如「今日 -48% 来自 1 合 5」
);

------------------------------------------------------------
-- 运营报告快照。每跑一次留一行，用来做跨期对比。
-- 不存进 dq_results 是因为那张表是「命中明细」，这张是「期末汇总」，
-- 明细会被规则调整改写，汇总必须冻结在当时的口径上才有对比意义。
------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dq_report_snapshot (
    snapshot_id   VARCHAR PRIMARY KEY,   -- 快照日期，一天一行，重跑覆盖当天
    taken_at      TIMESTAMP,
    data_date     DATE,                  -- 快照对应的最新交易日
    n_rules       BIGINT,
    n_hits        BIGINT,                -- 累计命中总数
    n_open        BIGINT,
    n_closed      BIGINT,                -- explained + fixed + ignored
    n_explained   BIGINT,
    n_fixed       BIGINT,
    n_ignored     BIGINT,
    avg_close_hrs DOUBLE,                -- 平均关闭时长，小时
    n_price_rows  BIGINT
);
