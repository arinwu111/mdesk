-- 校验规则种子。规则是数据不是代码，加一条规则等于加一行 INSERT。
--
-- 每条 sql_expr 必须且只能返回三列，顺序固定：symbol, biz_date, detail
--   symbol   命中的标的
--   biz_date 命中所属的业务日期，用于幂等去重
--   detail   给人看的一句话，说明命中了什么
--
-- note 字段务必写清这条规则的来历。半年后回看，这些 note 就是项目的演进史，
-- 也是面试时能讲出具体故事的地方。

------------------------------------------------------------
-- ref 域：主数据本身的正确性。代码错了后面全错，所以放在最前面。
------------------------------------------------------------

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-REF-001', 'ref', '证券代码有效性校验',
'自选股名单中的代码在数据源查不到任何行情，通常意味着代码写错、已退市或数据源不覆盖',
$rule$
SELECT w.symbol,
       CURRENT_DATE AS biz_date,
       '数据源无任何行情返回，代码可能无效、已退市或不在覆盖范围：' || w.name AS detail
FROM ref_watchlist w
LEFT JOIN (SELECT DISTINCT symbol FROM price_primary) p ON p.symbol = w.symbol
WHERE p.symbol IS NULL
$rule$,
'P0', true, DATE '2026-08-16',
'建库第一条规则。起因是整理自选股时发现用户给的名单里 MBIS/HOLD 是笔误、DRAM/SKHY 无法确认、智谱缺代码。证券代码本身就是需要校验的数据。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-REF-002', 'ref', '证券类型登记一致性',
'自选股名单里登记的品种类型与数据源返回的 quoteType 不一致，会导致后续按类型分流的规则全部走错分支',
$rule$
SELECT f.symbol,
       f.snapshot_date AS biz_date,
       '登记为 ' || w.instrument_type || ' 但数据源 quoteType 为 ' ||
       coalesce(f.quote_type, '空') || '：' || w.name AS detail
FROM raw_fundamental f
JOIN ref_watchlist w ON w.symbol = f.symbol
WHERE f.snapshot_date = (SELECT max(snapshot_date) FROM raw_fundamental)
  AND (
        (w.instrument_type = 'etf' AND coalesce(f.quote_type, '') <> 'ETF')
     OR (w.instrument_type IN ('stock', 'adr', 'sdr') AND coalesce(f.quote_type, '') = 'ETF')
  )
$rule$,
'P1', true, DATE '2026-08-16',
'ETF 没有 EPS 和 PE 是正常的，股票没有才是异常。若类型登记错了，字段缺失类规则会大面积误报。'
);

------------------------------------------------------------
-- price 域：行情数据本身的自洽性
------------------------------------------------------------

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-PRC-001', 'price', 'OHLC 逻辑越界',
'最高价低于最低价，或开盘收盘价落在最高最低区间之外。这类错误一定是数据问题，不可能是真实行情',
$rule$
SELECT symbol,
       trade_date AS biz_date,
       'OHLC 越界 O=' || round(open, 3)::VARCHAR ||
       ' H=' || round(high, 3)::VARCHAR ||
       ' L=' || round(low, 3)::VARCHAR ||
       ' C=' || round(close, 3)::VARCHAR || '（来源 ' || source || '）' AS detail
FROM raw_price_daily
-- 相对容忍带 1e-6。不加的话浮点存储误差会造成伪报，见下方来历说明
WHERE high < low  * (1 - 1e-6)
   OR close > high * (1 + 1e-6) OR close < low  * (1 - 1e-6)
   OR open  > high * (1 + 1e-6) OR open  < low  * (1 - 1e-6)
$rule$,
'P0', true, DATE '2026-08-16',
'最基础的自洽性检查。这条规则刻意扫描全部数据源而不只是主源，因为它是判断一个新接入的源'
'干不干净最快的办法。接入 akshare 后立刻命中 2 条：腾讯 C=445.39999 比 L=445.4 小、'
'阿里 C=130.60001 比 H=130.6 大，差值都在一亿分之一量级，是浮点存储误差不是数据错误。'
'零容忍地比较浮点数必然伪报，因此加入 1e-6 的相对容忍带。'
'真正的 OHLC 越界差值至少在分位上，不会被这个容忍带掩盖。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-PRC-002', 'price', '无公司行动的异常跳空',
'单日涨跌幅超过该标的自身近 120 日波动率的 6 倍，且当日无任何公司行动记录。'
'阈值按标的自身波动率分层，不用统一百分比',
$rule$
WITH d AS (
    SELECT symbol, trade_date, close,
           lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close
    FROM price_primary
    WHERE source = 'yfinance' AND close > 0
),
r AS (
    SELECT symbol, trade_date, close, prev_close,
           close / prev_close - 1 AS ret,
           -- 用命中日之前的 120 个交易日算波动率，不含当日，避免异常值抬高自己的阈值
           stddev_samp(close / prev_close - 1) OVER (
               PARTITION BY symbol ORDER BY trade_date
               ROWS BETWEEN 120 PRECEDING AND 1 PRECEDING
           ) AS sigma,
           count(*) OVER (
               PARTITION BY symbol ORDER BY trade_date
               ROWS BETWEEN 120 PRECEDING AND 1 PRECEDING
           ) AS n_hist
    FROM d
    WHERE prev_close IS NOT NULL AND prev_close > 0
)
SELECT r.symbol,
       r.trade_date AS biz_date,
       '较前收盘 ' || round(r.ret * 100, 2)::VARCHAR ||
       '%，达该标的近 ' || r.n_hist::VARCHAR || ' 日波动率(' ||
       round(r.sigma * 100, 2)::VARCHAR || '%)的 ' ||
       round(abs(r.ret) / r.sigma, 1)::VARCHAR ||
       ' 倍，当日无公司行动记录可解释' AS detail
FROM r
LEFT JOIN events_corporate_action ca
       ON ca.symbol = r.symbol AND ca.ex_date = r.trade_date
WHERE r.sigma > 0
  AND r.n_hist >= 60          -- 历史不足 60 日的次新股不参与，样本太少算不准
  AND abs(r.ret) > 6 * r.sigma
  AND abs(r.ret) > 0.05       -- 低波动标的的绝对下限，防止 6σ 落到 1% 以内刷屏
  AND ca.action_id IS NULL
$rule$,
'P1', true, DATE '2026-08-17',
'首版对全部标的统一取 20% 固定阈值，跑出 81 条命中，其中智谱 14 条、OKLO 12 条、IONQ 10 条，'
'三只高波动标的占了 44%，全是正常波动。而中国石油这类日波动率仅 1.5% 的标的，涨 20% 早就是重大事件，'
'固定阈值对它又太松。改为按标的自身近 120 日滚动标准差的 6 倍分层：高波动标的阈值自动放宽，'
'低波动标的自动收紧。另加 5% 绝对下限，避免极低波动标的的 6σ 落进日常波动区间。'
'历史不足 60 个交易日的次新股不参与本规则，样本量不够算不出可信的波动率。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-PRC-003', 'price', '个股零成交量但价格变动',
'单只标的成交量为零却有价格变化，且同市场其他标的当日成交正常。孤立出现通常意味着该股停牌而报价仍在推送',
$rule$
WITH d AS (
    SELECT p.symbol, w.market, p.trade_date, p.close, p.volume,
           lag(p.close) OVER (PARTITION BY p.symbol ORDER BY p.trade_date) AS prev_close
    FROM price_primary p
    JOIN ref_watchlist w ON w.symbol = p.symbol
),
flagged AS (
    SELECT * FROM d
    WHERE coalesce(volume, 0) = 0
      AND prev_close IS NOT NULL AND prev_close > 0
      AND abs(close / prev_close - 1) > 0.001
),
market_size AS (
    SELECT market, count(*) AS n FROM ref_watchlist GROUP BY market
)
SELECT f.symbol,
       f.trade_date AS biz_date,
       '成交量为 0 但收盘价较前日变动 ' ||
       round((f.close / f.prev_close - 1) * 100, 2)::VARCHAR ||
       '%，同市场当日仅此 ' ||
       (SELECT count(*) FROM flagged g
         WHERE g.market = f.market AND g.trade_date = f.trade_date)::VARCHAR ||
       ' 只出现，疑似个股停牌' AS detail
FROM flagged f
JOIN market_size m ON m.market = f.market
WHERE (SELECT count(*) FROM flagged g
        WHERE g.market = f.market AND g.trade_date = f.trade_date) < m.n * 0.5
$rule$,
'P1', true, DATE '2026-08-16',
'初版不区分个股与全市场，港股半日市当天十来只标的一起命中，噪声很大。加上同市场同日命中占比的判据后，只保留孤立出现的情况，全市场性的交给 DQ-PRC-005。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-PRC-005', 'price', '全市场成交量缺失',
'同一交易日某个市场过半标的成交量都为零。这不是行情异常，是数据源当日的成交量字段整体缺失',
$rule$
WITH d AS (
    SELECT p.symbol, w.market, p.trade_date, p.volume
    FROM price_primary p
    JOIN ref_watchlist w ON w.symbol = p.symbol
),
agg AS (
    SELECT market, trade_date,
           count(*) AS total,
           count(*) FILTER (WHERE coalesce(volume, 0) = 0) AS zeros
    FROM d GROUP BY 1, 2
)
SELECT market AS symbol,
       trade_date AS biz_date,
       market || ' 市场当日 ' || total::VARCHAR || ' 只标的中有 ' ||
       zeros::VARCHAR || ' 只成交量为 0，数据源成交量整体缺失，当日成交量不可用' AS detail
FROM agg
WHERE total >= 4 AND zeros >= total * 0.5
$rule$,
'P2', true, DATE '2026-08-16',
'首轮命中 6 天，全部是香港半日市：平安夜、除夕、农历新年前一日。港交所这几天只有早市，数据源不返回成交量。这属于已知的市场规则而非数据错误，定为 P2 观察级，处理方式是在这些日期跳过成交量相关计算。半日市日历需要每年从港交所公告更新一次，已写入 SOP 的年度事项。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-PRC-004', 'price', '交易日序列断档',
'相邻两个交易日间隔超过 10 个自然日。可能是长期停牌、数据源缺失，也可能是上市后有过退市重组',
$rule$
WITH d AS (
    SELECT symbol, trade_date,
           lag(trade_date) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_date
    FROM price_primary
)
SELECT symbol,
       trade_date AS biz_date,
       '与上一交易日 ' || prev_date::VARCHAR || ' 间隔 ' ||
       date_diff('day', prev_date, trade_date)::VARCHAR || ' 个自然日' AS detail
FROM d
WHERE prev_date IS NOT NULL
  AND date_diff('day', prev_date, trade_date) > 10
$rule$,
'P2', true, DATE '2026-08-16',
'长假会误报，所以定为 P2 观察级。真正要抓的是长期停牌和历史断层，比如 Nebius 前身停牌两年多后复牌改代码这种情况。'
);

------------------------------------------------------------
-- corp_action 域：公司行动与复权。这个域是整个项目的核心
------------------------------------------------------------

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-CA-001', 'corp_action', '数据源派息金额与其复权价不自洽',
'从数据源自己的复权价反推出的隐含派息，与它自己上报的派息金额对不上。这是纯内部矛盾，不需要任何外部资料就能判定其中一个是错的',
$rule$
WITH p AS (
    SELECT symbol, trade_date, close, adj_close,
           adj_close / close AS ratio,
           lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close,
           lag(adj_close / close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_ratio
    FROM price_primary
    WHERE close > 0 AND adj_close IS NOT NULL AND adj_close > 0
),
implied AS (
    SELECT p.symbol, p.trade_date AS ex_date, ca.amount, ca.currency,
           p.prev_close * (1 - p.prev_ratio / p.ratio) AS implied_div
    FROM p
    JOIN events_corporate_action ca
      ON ca.symbol = p.symbol AND ca.ex_date = p.trade_date
     AND ca.action_type = 'cash_dividend'
    WHERE ca.amount IS NOT NULL AND p.prev_ratio IS NOT NULL
)
SELECT symbol,
       ex_date AS biz_date,
       '数据源自身不自洽：上报每股派息 ' || round(amount, 6)::VARCHAR || ' ' ||
       coalesce(currency, '') || '，但其复权价隐含派息仅 ' ||
       round(implied_div, 6)::VARCHAR || '，相差 ' ||
       round(amount / implied_div, 3)::VARCHAR || ' 倍' AS detail
FROM implied
WHERE implied_div > 0
  AND abs(amount / implied_div - 1) > 0.01
$rule$,
'P0', true, DATE '2026-08-16',
'项目的核心规则，也是第一条真正抓到东西的规则。首轮就命中阿里 9988.HK 两次派息全错、汇丰 0005.HK 最近一次派息错，隐含倍数均为 7.847，恰等于港币兑美元联系汇率，说明数据源用美元金额去扣减港币价格。两只中招的都是有美股 ADR 的港股。初版规则按受影响的每个交易日报警，阿里一个根因刷出 445 条，改为按除权日定位根因后降到 3 条。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-CA-004', 'corp_action', '自算复权序列整体偏离',
'按公司行动自行推算的复权价与数据源整段偏离。根因通常已由 DQ-CA-001 指出，这条负责说明影响范围有多大',
$rule$
SELECT symbol,
       max(trade_date) AS biz_date,
       '全历史 ' || count(*)::VARCHAR || ' 个交易日中有 ' ||
       count(*) FILTER (WHERE abs(diff_pct) > 0.5)::VARCHAR ||
       ' 日复权价与数据源偏离，最大 ' ||
       round(max(abs(diff_pct)), 3)::VARCHAR || '%，历史涨跌幅计算会受影响' AS detail
FROM mart_adj_price
WHERE src_adj IS NOT NULL AND self_adj IS NOT NULL
GROUP BY symbol
HAVING count(*) FILTER (WHERE abs(diff_pct) > 0.5) > 0
$rule$,
'P1', true, DATE '2026-08-16',
'从 DQ-CA-001 拆出来的。根因和影响范围要分开报：根因一条，用于定位和修复；影响范围一条，用于判断这只标的的历史数据还能不能用。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-CA-002', 'corp_action', '拆合股比例超出常理',
'拆股比例大于 100 或小于 0.01。这个量级的比例极其罕见，更可能是数据源填反了方向或单位错位',
$rule$
SELECT symbol,
       ex_date AS biz_date,
       action_type || ' 比例上报为 ' || ratio::VARCHAR || '，超出常理范围' AS detail
FROM events_corporate_action
WHERE ratio IS NOT NULL
  AND (ratio > 100 OR ratio < 0.01)
$rule$,
'P0', true, DATE '2026-08-16',
'拆股比例填反是复权出错最隐蔽的原因，因为价格曲线看起来仍然连续，只是整段被缩放了。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-CA-003', 'corp_action', '派息金额超过股价一成',
'单次现金派息超过除权日前收盘价的 10%。可能是特别股息，也可能是币种没换算或者金额单位错',
$rule$
WITH prev AS (
    SELECT ca.action_id, ca.symbol, ca.ex_date, ca.amount, ca.currency,
           (SELECT p.close FROM price_primary p
             WHERE p.symbol = ca.symbol AND p.trade_date < ca.ex_date
             ORDER BY p.trade_date DESC LIMIT 1) AS prev_close
    FROM events_corporate_action ca
    WHERE ca.action_type = 'cash_dividend' AND ca.amount IS NOT NULL
)
SELECT symbol,
       ex_date AS biz_date,
       '每股派息 ' || round(amount, 4)::VARCHAR || ' ' || coalesce(currency, '') ||
       '，占除权前收盘价 ' || round(amount / prev_close * 100, 2)::VARCHAR || '%' AS detail
FROM prev
WHERE prev_close IS NOT NULL AND prev_close > 0
  AND amount / prev_close > 0.10
$rule$,
'P1', true, DATE '2026-08-16',
'高股息标的如中国石油会正常命中，需要人工确认是特别股息还是数据错。这类规则的价值不在于零误报，在于强制你每次都去看一眼。'
);

------------------------------------------------------------
-- fundamental 域：估值与财务字段
-- 关键是区分「本就不适用」和「真缺失」，前者不是异常
------------------------------------------------------------

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-FUN-001', 'fundamental', '市值或股本缺失',
'非 ETF 标的缺少市值或总股本。这两个字段任何一只正常交易的股票都不该缺',
$rule$
SELECT f.symbol,
       f.snapshot_date AS biz_date,
       '缺失字段：' ||
       CASE WHEN f.market_cap IS NULL THEN '市值 ' ELSE '' END ||
       CASE WHEN f.shares_outstanding IS NULL THEN '总股本' ELSE '' END ||
       '（' || w.name || '）' AS detail
FROM raw_fundamental f
JOIN ref_watchlist w ON w.symbol = f.symbol
WHERE f.snapshot_date = (SELECT max(snapshot_date) FROM raw_fundamental)
  AND w.instrument_type <> 'etf'
  AND (f.market_cap IS NULL OR f.shares_outstanding IS NULL)
$rule$,
'P1', true, DATE '2026-08-16',
'预期 SDR 和次新股会命中。SKHY 是存托凭证结构、智谱和紫金黄金国际是次新股，免费数据源对这类标的的基本面覆盖通常最差。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-FUN-002', 'fundamental', '盈利为正却没有市盈率',
'EPS 大于零但 PE 为空。EPS 为负导致 PE 为空是正常的，EPS 为正还没有 PE 就是真缺失',
$rule$
SELECT f.symbol,
       f.snapshot_date AS biz_date,
       'EPS 为 ' || round(f.eps_ttm, 4)::VARCHAR || ' 但 PE(TTM) 为空：' || w.name AS detail
FROM raw_fundamental f
JOIN ref_watchlist w ON w.symbol = f.symbol
WHERE f.snapshot_date = (SELECT max(snapshot_date) FROM raw_fundamental)
  AND w.instrument_type <> 'etf'
  AND f.eps_ttm IS NOT NULL AND f.eps_ttm > 0
  AND f.pe_ttm IS NULL
$rule$,
'P1', true, DATE '2026-08-16',
'这条规则是为了把「无盈利所以没有 PE」和「有盈利但字段丢了」分开。优必选、智谱、OKLO 这类无盈利标的不该因为 PE 为空被告警。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-FUN-003', 'fundamental', '估值指标极端值',
'PE 超过 1000 或 PB 超过 100。可能是真实的极端估值，也可能是分母接近零或单位错位',
$rule$
SELECT f.symbol,
       f.snapshot_date AS biz_date,
       '估值超出常规区间 PE=' || coalesce(round(f.pe_ttm, 1)::VARCHAR, '空') ||
       ' PB=' || coalesce(round(f.pb, 1)::VARCHAR, '空') || '：' || w.name AS detail
FROM raw_fundamental f
JOIN ref_watchlist w ON w.symbol = f.symbol
WHERE f.snapshot_date = (SELECT max(snapshot_date) FROM raw_fundamental)
  AND (f.pe_ttm > 1000 OR f.pb > 100)
$rule$,
'P2', true, DATE '2026-08-16',
'微利公司的 PE 天然会很大，所以定 P2。真正要防的是分母单位错位，比如把千元当成元。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-FUN-004', 'fundamental', '净资产为负或市销率异常',
'市净率为负说明账面净资产为负，市销率畸高说明营收极小或口径有误。'
'两者都可能是真实情况，但都必须人工确认一次而不是默认接受',
$rule$
SELECT f.symbol,
       f.snapshot_date AS biz_date,
       CASE WHEN f.pb < 0
            THEN '市净率为负 (' || round(f.pb, 2)::VARCHAR || ')，账面净资产为负：' || w.name
            ELSE '市销率高达 ' || round(f.ps_ttm, 1)::VARCHAR || '，营收极小或口径存疑：' || w.name
       END AS detail
FROM raw_fundamental f
JOIN ref_watchlist w ON w.symbol = f.symbol
WHERE f.snapshot_date = (SELECT max(snapshot_date) FROM raw_fundamental)
  AND w.instrument_type <> 'etf'
  AND (f.pb < 0 OR f.ps_ttm > 100)
$rule$,
'P1', true, DATE '2026-08-17',
'建个股页的关键指标面板时发现智谱 PB 为 -54.32、PS 为 816，现有规则一条都没命中——'
'DQ-FUN-003 只查了 PE 和 PB 的上限，没查负值，等于默认接受了净资产为负这种情况。'
'负 PB 对刚上市的科技公司可能是可转换优先股会计处理的正常结果，也可能是真的资不抵债，'
'两种情况的含义天差地别，必须人工确认一次。这条规则是「先做界面、再发现规则漏洞」的例子。'
);

------------------------------------------------------------
-- cross 域：跨市场校验
-- 同一家公司在两地上市，折算后价格应当收敛。不收敛就是有一边错了
------------------------------------------------------------

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-CRS-001', 'cross', '跨市场折算价差超阈值',
'两地上市标的按存托比例和汇率折算后仍偏离超过各自设定的阈值。可能是比例错、汇率错、价格错，也可能是真实溢价',
$rule$
WITH last_px AS (
    SELECT symbol, trade_date, close
    FROM price_primary
    QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) = 1
),
last_fx AS (
    SELECT pair, rate
    FROM raw_fx_daily
    QUALIFY row_number() OVER (PARTITION BY pair ORDER BY trade_date DESC) = 1
),
calc AS (
    SELECT c.pair_id, c.symbol_a, c.symbol_b, c.ratio, c.tolerance_pct,
           a.close AS px_a, b.close AS px_b, a.trade_date,
           coalesce(f.rate, 1.0) AS fx,
           b.close / nullif(a.close * c.ratio * coalesce(f.rate, 1.0), 0) - 1 AS dev
    FROM ref_cross_listing c
    JOIN last_px a ON a.symbol = c.symbol_a
    JOIN last_px b ON b.symbol = c.symbol_b
    LEFT JOIN last_fx f ON f.pair = c.fx_pair
)
SELECT symbol_a AS symbol,
       trade_date AS biz_date,
       pair_id || ' 折算后偏离 ' || round(dev * 100, 2)::VARCHAR ||
       '%（阈值 ' || tolerance_pct::VARCHAR || '%，' ||
       symbol_a || '=' || round(px_a, 3)::VARCHAR || ' ' ||
       symbol_b || '=' || round(px_b, 3)::VARCHAR || '）' AS detail
FROM calc
WHERE dev IS NOT NULL
  AND abs(dev * 100) > tolerance_pct
$rule$,
'P0', true, DATE '2026-08-16',
'阈值按对子分别设定：双重主要上市如阿里、小鹏取 3%，ADR 溢价常态化的台积电取 25%，A+H 溢价属正常现象所以放到 60% 以上，监控的是溢价突变而非溢价本身。'
);

------------------------------------------------------------
-- source 域：跨源比对
--
-- 前面所有域查的都是「内部自洽」——数据源自己跟自己有没有矛盾。
-- 但如果一个源给出的值看起来完全合理、内部也自洽，只是数值本身就是错的，
-- 单源永远查不出来。这个域的存在就是为了补这个盲区。
-- 主源 yfinance，副源 akshare（港股走新浪，美股走新浪），两条链路互相独立。
------------------------------------------------------------

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-SRC-001', 'source', '两源收盘价不一致',
'同一标的同一交易日，主源与副源给出的收盘价偏离超过 0.5%。'
'两个独立数据源不该对同一个已收盘的事实有分歧，有分歧就至少有一方错了',
$rule$
SELECT a.symbol,
       a.trade_date AS biz_date,
       '收盘价两源不一致：yfinance ' || round(a.close, 4)::VARCHAR ||
       ' vs akshare ' || round(b.close, 4)::VARCHAR ||
       '，偏离 ' || round((a.close / b.close - 1) * 100, 3)::VARCHAR || '%' AS detail
FROM raw_price_daily a
JOIN raw_price_daily b
  ON b.symbol = a.symbol AND b.trade_date = a.trade_date AND b.source = 'akshare'
WHERE a.source = 'yfinance'
  AND a.close > 0 AND b.close > 0
  AND abs(a.close / b.close - 1) > 0.005
$rule$,
'P0', true, DATE '2026-08-17',
'跨源域的第一条规则，也是这个域存在的理由。阈值取 0.5% 而不是 0，'
'因为两源的收盘价小数位精度和港股半日市收盘口径可能有细微差别，'
'留出容忍带才能让真正的分歧浮出来。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-SRC-002', 'source', '两源成交量差异过大',
'同一交易日两源成交量相差超过 20%。成交量口径分歧常见于是否计入暗盘、盘后交易与大宗交易',
$rule$
SELECT a.symbol,
       a.trade_date AS biz_date,
       '成交量两源差异：yfinance ' || a.volume::VARCHAR ||
       ' vs akshare ' || b.volume::VARCHAR ||
       '，相差 ' || round((a.volume::DOUBLE / b.volume - 1) * 100, 1)::VARCHAR || '%' AS detail
FROM raw_price_daily a
JOIN raw_price_daily b
  ON b.symbol = a.symbol AND b.trade_date = a.trade_date AND b.source = 'akshare'
WHERE a.source = 'yfinance'
  AND coalesce(a.volume, 0) > 0 AND coalesce(b.volume, 0) > 0
  AND abs(a.volume::DOUBLE / b.volume - 1) > 0.20
$rule$,
'P1', true, DATE '2026-08-17',
'成交量比收盘价更容易有口径差异，所以定 P1 而不是 P0。'
'真正要抓的是某一源系统性地少算或多算，而不是个别日期的零星出入。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-SRC-003', 'source', '交易日在两源间不一致',
'某个交易日只有一个源有数据。可能是一方漏采，也可能是对停牌日、半日市的处理口径不同',
$rule$
WITH span AS (
    -- 只比对两源都有覆盖的重叠区间，否则副源回溯窗口较短会全量误报
    SELECT symbol,
           greatest(min(trade_date) FILTER (WHERE source = 'yfinance'),
                    min(trade_date) FILTER (WHERE source = 'akshare')) AS lo,
           least(max(trade_date) FILTER (WHERE source = 'yfinance'),
                 max(trade_date) FILTER (WHERE source = 'akshare')) AS hi
    FROM raw_price_daily
    GROUP BY symbol
    HAVING count(*) FILTER (WHERE source = 'akshare') > 0
),
d AS (
    SELECT p.symbol, p.trade_date,
           count(*) FILTER (WHERE p.source = 'yfinance') AS n_yf,
           count(*) FILTER (WHERE p.source = 'akshare')  AS n_ak
    FROM raw_price_daily p
    JOIN span s ON s.symbol = p.symbol
               AND p.trade_date BETWEEN s.lo AND s.hi
    GROUP BY 1, 2
)
SELECT symbol,
       trade_date AS biz_date,
       CASE WHEN n_ak = 0 THEN '该交易日仅 yfinance 有数据，akshare 缺失'
            ELSE '该交易日仅 akshare 有数据，yfinance 缺失' END AS detail
FROM d
WHERE n_yf = 0 OR n_ak = 0
$rule$,
'P1', true, DATE '2026-08-17',
'只在两源重叠的日期区间内比对。副源只回溯 120 天，不限定区间的话'
'会把副源尚未覆盖的历史全部报成缺失，那是噪声不是问题。'
);

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-SRC-004', 'source', '标的未被副源覆盖',
'自选股中的标的在副源完全查不到。不影响当前数据可用性，但这些标的失去了跨源校验能力',
$rule$
SELECT w.symbol,
       CURRENT_DATE AS biz_date,
       w.name || ' 在副源 akshare 无任何数据，该标的无法进行跨源校验' AS detail
FROM ref_watchlist w
LEFT JOIN (SELECT DISTINCT symbol FROM raw_price_daily WHERE source = 'akshare') a
       ON a.symbol = w.symbol
WHERE NOT w.is_reference AND a.symbol IS NULL
$rule$,
'P2', true, DATE '2026-08-17',
'定 P2 是因为它不代表数据错，只代表这只标的的校验强度比别的低。'
'首轮东财链路失败导致 8 只港股未覆盖，改用新浪链路后归零。'
);

------------------------------------------------------------
-- ops 域：采集作业自身的健康度
-- 数据没拉到，前面所有规则都会静默通过，所以这个域必须有
------------------------------------------------------------

INSERT OR REPLACE INTO dq_rules VALUES (
'DQ-OPS-001', 'ops', '采集作业未成功',
'最近一轮采集中有标的的某个数据集返回失败或为空。数据没拉到时其他规则会静默通过，这是最危险的情况',
$rule$
SELECT symbol,
       fetched_at::DATE AS biz_date,
       dataset || ' 采集状态 ' || status || '：' || coalesce(message, '') AS detail
FROM raw_fetch_log
WHERE run_id = (SELECT max(run_id) FROM raw_fetch_log)
  AND status <> 'ok'
  -- 两年内没有派息也没有拆股是常态，不是采集失败
  AND NOT (dataset = 'corp_action' AND status = 'empty')
$rule$,
'P0', true, DATE '2026-08-16',
'监控系统自己也需要被监控。静默失败比报错更危险，因为看板照常显示，只是数字停在了昨天。首轮跑完发现 20 只标的两年内无任何公司行动被判为 empty 而误报，已排除该组合。采集层只记录事实，判断什么算异常是规则层的事。'
);
