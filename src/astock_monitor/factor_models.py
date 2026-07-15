from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FactorDefinition:
    key: str
    name: str
    category: str
    formula: str
    explanation: str
    direction: str = "越大越好"
    data_requirement: str = "本地日线"
    executable_formula: str = ""
    caveat: str = ""

    @property
    def executable(self) -> bool:
        return bool(self.executable_formula)


@dataclass(frozen=True, slots=True)
class FactorModelDefinition:
    key: str
    name: str
    category: str
    factor_keys: tuple[str, ...]
    formula: str
    explanation: str
    entry_formula: str = ""
    exit_formula: str = ""
    score_formula: str = ""
    data_requirement: str = "本地日线"

    @property
    def executable(self) -> bool:
        return bool(self.entry_formula and self.score_formula)


def _f(
    key: str,
    name: str,
    category: str,
    formula: str,
    explanation: str,
    direction: str = "越大越好",
    data: str = "本地日线",
    executable: str = "",
    caveat: str = "",
) -> FactorDefinition:
    return FactorDefinition(
        key,
        name,
        category,
        formula,
        explanation,
        direction,
        data,
        executable,
        caveat,
    )


# 公式、解释和分类来自《A股量化因子公式与平台实现指南》。展示公式保留研究
# 含义；executable_formula 仅在现有本地日线字段足以真实计算时提供。
FACTOR_CATALOG: tuple[FactorDefinition, ...] = (
    # 估值
    _f("ep", "盈利收益率 EP", "估值因子", "EP = NP_TTM / MC", "最近十二个月归母净利润相对于总市值的比例，是市盈率倒数。它衡量每单位市值对应的盈利，常用于低估值选股。", data="公告日可用的TTM利润 + 总市值", caveat="净利润为负时通常记为缺失或单独标记，不能与正常正盈利公司直接排序。"),
    _f("bp", "账面市值比 BP", "估值因子", "BP = Equity / MC = 1 / PB", "归母净资产与总市值之比，反映市场价格相对账面价值的高低，是价值因子和 HML 的核心输入。", data="公告日可用净资产 + 总市值", caveat="净资产为负属于异常样本；金融行业与普通行业宜分组比较。"),
    _f("sp", "销售收益率 SP", "估值因子", "SP = Revenue_TTM / MC", "最近十二个月营业收入与总市值之比。相较 EP，它不受利润率短期波动影响，但需要结合行业利润率解读。", data="公告日可用TTM营收 + 总市值"),
    _f("cfp", "经营现金流收益率 CFP", "估值因子", "CFP = CFO_TTM / MC", "经营活动现金流净额相对于总市值的比例，用现金实现能力补充利润口径估值。", data="公告日可用TTM经营现金流 + 总市值", caveat="周期性营运资本变化会造成短期现金流大幅波动。"),
    _f("dividend_yield", "股息率", "估值因子", "DividendYield = CashDividend_12M / MC", "过去十二个月现金分红相对于当前市值的比例，反映已实现的现金回报。", data="分红事件 + 总市值", caveat="除息日和公告日必须按时点处理，不能使用未来已知分红。"),
    _f("ebitda_yield", "EBITDA 企业价值收益率", "估值因子", "EBITDAYield = EBITDA_TTM / EV", "用息税折旧摊销前利润除以企业价值，降低资本结构与折旧政策对跨公司的影响。", data="财务报表 + 市值 + 有息负债 + 现金"),
    _f("fcf_yield", "自由现金流收益率", "估值因子", "FCFYield = FCF_TTM / MC；FCF = CFO - CAPEX", "自由现金流相对于市值的比例，关注企业在维持经营和资本开支后可自由支配的现金。", data="现金流量表 + 资本开支 + 市值", caveat="资本开支字段需统一口径，金融企业不宜机械套用。"),
    _f("hml", "HML 价值组合", "估值因子", "HML = Return(高BP组合) - Return(低BP组合)", "Fama-French 价值因子收益。先按 BP 分组，再计算高账面市值比组合减低账面市值比组合的收益。", data="截面BP + 组合收益", caveat="它是因子组合收益，不是单只股票原始因子。"),
    # 规模
    _f("market_cap", "总市值 MC", "规模因子", "MC = Price × TotalShares", "股票价格乘总股本，衡量公司的整体权益规模。", data="实时/日线价格 + 总股本"),
    _f("float_market_cap", "流通市值 FMC", "规模因子", "FMC = Price × FreeFloatShares", "价格乘自由流通股本，更接近可交易供给规模，常用于流动性与容量约束。", data="实时/日线价格 + 自由流通股本"),
    _f("log_size", "对数规模", "规模因子", "Size = ln(MC)", "对总市值取自然对数，压缩极端大市值差异，是中性化回归的经典规模暴露。", data="总市值"),
    _f("small_size", "小市值暴露", "规模因子", "Small = -ln(MC)", "对数规模取负值，使数值越大代表公司越小，便于与其他正向分数合成。", data="总市值"),
    _f("smb", "SMB 规模组合", "规模因子", "SMB = Return(小市值组合) - Return(大市值组合)", "Fama-French 规模因子收益，小市值组合收益减大市值组合收益。", data="截面市值 + 组合收益", caveat="它是组合收益，需固定股票池和再平衡规则。"),
    # 质量
    _f("roe", "净资产收益率 ROE", "质量因子", "ROE = NP_TTM / Equity", "归母净利润相对股东权益的回报率，综合反映盈利能力与资本使用效率。", data="公告日可用财务报表", caveat="高杠杆或极低净资产会机械抬高 ROE。"),
    _f("roa", "总资产收益率 ROA", "质量因子", "ROA = NetProfit_TTM / Assets", "净利润相对于总资产的比例，弱化资本结构差异后衡量资产盈利效率。", data="公告日可用财务报表"),
    _f("gross_margin", "毛利率", "质量因子", "GM = (Revenue - Cost) / Revenue", "每单位营业收入扣除营业成本后的剩余比例，反映产品议价能力和成本控制。", data="利润表"),
    _f("net_margin", "净利率", "质量因子", "NM = NetProfit / Revenue", "净利润相对于营业收入的比例，反映企业最终盈利转化能力。", data="利润表"),
    _f("asset_turnover", "总资产周转率", "质量因子", "AT = Revenue / Assets", "单位资产创造收入的能力。资本密集行业通常天然较低，应做行业中性化。", data="利润表 + 资产负债表"),
    _f("gpa", "毛利润/总资产 GP/A", "质量因子", "GP/A = (Revenue - Cost) / Assets", "Novy-Marx 盈利质量指标，用资产基础衡量毛利润创造能力。", data="利润表 + 资产负债表"),
    _f("cfoa", "经营现金流/总资产 CFO/A", "质量因子", "CFO/A = CFO_TTM / Assets", "用经营现金流相对总资产衡量现金型盈利能力。", data="现金流量表 + 资产负债表"),
    _f("cash_quality", "现金盈利质量", "质量因子", "CashQuality = CFO_TTM / NP_TTM", "比较经营现金流和会计利润。长期显著低于 1 可能意味着利润现金含量偏弱。", data="利润表 + 现金流量表", caveat="净利润接近零或为负时比值失真。"),
    _f("accrual", "应计利润", "质量因子", "Accrual = (NP_TTM - CFO_TTM) / Assets", "会计利润中未转化为经营现金流的部分。一般数值越低，盈利现金质量越高。", "越小越好", "利润表 + 现金流量表 + 资产负债表"),
    _f("leverage", "资产负债率", "质量因子", "Leverage = Liabilities / Assets", "负债占总资产的比例，反映财务杠杆和偿债压力。", "越小越好", "资产负债表"),
    _f("interest_coverage", "利息保障倍数", "质量因子", "ICR = EBIT / InterestExpense", "息税前利润覆盖利息费用的倍数，数值越高通常表示债务利息承受能力越强。", data="利润表"),
    _f("roe_stability", "ROE 稳定性", "质量因子", "ROEStability = -Std(QuarterlyROE)", "对季度 ROE 波动率取负，奖励盈利能力更稳定的公司。", data="历史季度财务快照"),
    _f("quality_composite", "综合质量评分", "质量因子", "Quality = Σ wᵢ × Z(Qualityᵢ)", "将 ROE、GP/A、CFO/A 等正向指标和应计、杠杆等反向指标去极值、标准化后加权合成。", data="多项财务因子截面", caveat="权重、行业中性化和缺失值规则必须固化并记录版本。"),
    # 成长
    _f("revenue_growth", "营业收入同比增长", "成长因子", "RevGrowth = Revenue_TTM / Revenue_TTM-1Y - 1", "最近十二个月营业收入相对一年前的增长率，衡量业务规模扩张。", data="公告日可用历史财务报表"),
    _f("profit_growth", "净利润同比增长", "成长因子", "NPGrowth = NP_TTM / NP_TTM-1Y - 1", "最近十二个月净利润相对一年前的增长率，衡量盈利扩张。", data="公告日可用历史财务报表", caveat="基期为负、接近零或利润转正时需单独分类。"),
    _f("eps_growth", "EPS 同比增长", "成长因子", "EPSGrowth = EPS_TTM / EPS_TTM-1Y - 1", "每股收益的同比增速，兼顾利润变化与股本摊薄影响。", data="财务报表 + 历史股本"),
    _f("delta_roe", "ROE 改善", "成长因子", "ΔROE = ROE_t - ROE_t-1Y", "当前 ROE 相对一年前的变化，用于识别盈利效率改善。", data="历史季度财务快照"),
    _f("delta_gm", "毛利率改善", "成长因子", "ΔGM = GM_t - GM_t-1Y", "毛利率相对一年前的变化，反映产品结构、价格或成本改善。", data="历史季度利润表"),
    _f("asset_growth", "总资产增长", "成长因子", "AssetGrowth = Assets_t / Assets_t-1Y - 1", "总资产同比扩张速度。扩张可能来自有效投资，也可能带来较低未来回报，需结合质量分析。", data="历史资产负债表", caveat="在 Fama-French CMA 中，激进投资通常是反向暴露。"),
    _f("capex_growth", "资本开支增长", "成长因子", "CAPEXGrowth = CAPEX_t / CAPEX_t-1Y - 1", "资本性支出的增长速度，反映产能与长期资产投入。", data="历史现金流量表"),
    _f("sustainable_growth", "可持续增长率", "成长因子", "SGR = ROE × (1 - PayoutRatio)", "在盈利能力、分红率和杠杆结构不变时，企业依靠留存收益可支持的理论增长速度。", data="ROE + 分红率"),
    _f("rmw", "RMW 盈利组合", "成长因子", "RMW = Return(强盈利组合) - Return(弱盈利组合)", "Fama-French 五因子中的盈利能力组合收益。", data="截面盈利质量 + 组合收益", caveat="组合收益而非单股原始值。"),
    _f("cma", "CMA 投资组合", "成长因子", "CMA = Return(保守投资组合) - Return(激进投资组合)", "Fama-French 五因子中的投资风格组合收益，通常按资产增长率分组。", data="截面资产增长 + 组合收益", caveat="组合收益而非单股原始值。"),
    # 动量与技术
    _f("mom5", "5日动量", "动量与技术", "MOM_5 = P_t / P_t-5 - 1", "近 5 个交易日累计价格收益，反映极短周期趋势。", data="本地日线", executable="ROC(close,5)"),
    _f("mom20", "20日动量", "动量与技术", "MOM_20 = P_t / P_t-20 - 1", "近 20 个交易日累计收益，约对应一个交易月。", data="本地日线", executable="ROC(close,20)"),
    _f("mom60", "60日动量", "动量与技术", "MOM_60 = P_t / P_t-60 - 1", "近 60 个交易日累计收益，刻画中短期趋势。", data="本地日线", executable="ROC(close,60)"),
    _f("mom120", "120日动量", "动量与技术", "MOM_120 = P_t / P_t-120 - 1", "近 120 个交易日累计收益，约对应半年趋势。", data="本地日线", executable="ROC(close,120)"),
    _f("mom250", "250日动量", "动量与技术", "MOM_250 = P_t / P_t-250 - 1", "近 250 个交易日累计收益，约对应一年趋势。", data="本地日线", executable="ROC(close,250)"),
    _f("mom12_1", "12-1月动量", "动量与技术", "MOM_12-1 = P_t-21 / P_t-252 - 1", "跳过最近一个月后的过去一年动量，减少短期反转对中期动量的干扰。", data="本地日线", executable="ROC(REF(close,21),231)"),
    _f("rev5", "5日反转", "动量与技术", "REV_5 = -MOM_5", "短期收益取反，数值越高代表近期跌幅越大，用于研究短期均值回归。", data="本地日线", executable="-ROC(close,5)"),
    _f("high52", "52周高点接近度", "动量与技术", "High52 = P_t / max(P_t-251…P_t)", "当前价格相对过去约 52 周最高价的位置，越接近 1 表示价格越靠近年度高点。", data="本地日线", executable="close / HHV(close,252)"),
    _f("bias20", "20日乖离率 BIAS", "动量与技术", "BIAS_20 = P_t / MA_20 - 1", "当前价格偏离 20 日均线的比例，刻画趋势强度与短期过热程度。", data="本地日线", executable="(close / SMA(close,20) - 1) * 100"),
    _f("rsi14", "RSI(14)", "动量与技术", "RSI_N = 100 × AvgGain_N / (AvgGain_N + AvgLoss_N)", "比较窗口内上涨幅度和总波动幅度的相对强弱，常用 14 日。高值表示近期上涨动能较强，但不等于必然反转。", data="本地日线", executable="100 * SUM(MAX(returns,0),14) / SUM(ABS(returns),14)"),
    _f("macd_dif", "MACD DIF", "动量与技术", "DIF = EMA_12(P) - EMA_26(P)", "快慢指数均线之差，用于刻画中短期趋势方向与强度。", data="本地日线", executable="EMA(close,12) - EMA(close,26)"),
    _f("macd_hist", "MACD 柱", "动量与技术", "MACD = DIF - EMA_9(DIF)", "DIF 与其 9 日信号线之差，反映趋势动能的加速或减速。部分软件会将柱值乘 2。", data="本地日线", executable="(EMA(close,12)-EMA(close,26))-EMA(EMA(close,12)-EMA(close,26),9)"),
    _f("bollz20", "布林带标准分 BOLLZ", "动量与技术", "BOLLZ = (P - MA_20) / Std_20(P)", "价格相对 20 日均线的标准化偏离程度，可跨不同价格水平比较。", data="本地日线", executable="(close-SMA(close,20))/STD(close,20)"),
    _f("pv_corr20", "量价相关性", "动量与技术", "PVCorr = Corr(r, Δln(Volume))", "收益率与成交量对数变化的滚动相关系数，用于描述量价同向或背离。", data="本地日线", executable="CORR(returns,DIFF(LOG(MAX(volume,1)),1),20)"),
    # 风险
    _f("vol20", "20日年化波动率", "风险因子", "VOL_20 = Std(r,20) × √252", "日收益率标准差年化，衡量短期总波动风险。作为低波因子时通常取负值进入评分。", "越小越好", "本地日线", "STD(returns,20)*SQRT(252)"),
    _f("vol60", "60日年化波动率", "风险因子", "VOL_60 = Std(r,60) × √252", "用更长窗口估计年化波动率，稳定性通常高于 20 日口径。", "越小越好", "本地日线", "STD(returns,60)*SQRT(252)"),
    _f("downside_vol20", "20日下行波动率", "风险因子", "DownsideVol = √(252 × Avg(min(r,0)²))", "只统计负收益的平方，聚焦投资者真正关心的下行波动。", "越小越好", "本地日线", "SQRT(252*SUM(MIN(returns,0)*MIN(returns,0),20)/20)"),
    _f("beta", "市场 Beta", "风险因子", "Beta = Cov(r_stock,r_market) / Var(r_market)", "股票收益对市场收益的敏感度。Beta 大于 1 表示历史上相对市场波动更强。", "视策略而定", "股票与同口径基准收益", caveat="基准、复权、窗口和无风险收益口径必须保持一致。"),
    _f("ivol", "特质波动率 IVOL", "风险因子", "IVOL = Std(市场模型残差) × √252", "先用市场模型回归股票收益，再将残差波动年化，表示不能被市场共同波动解释的风险。", "越小越好", "股票与基准收益 + 回归"),
    _f("max_drawdown", "窗口最大回撤", "风险因子", "MDD = min(P_t / max(P_≤t) - 1)", "窗口内从历史高点到后续低点的最大跌幅，直观描述最严重的路径损失。", "越小越好", "本地日线"),
    _f("skew", "收益偏度", "风险因子", "Skew = E[(r-μ)³] / σ³", "收益分布的不对称程度。负偏度意味着历史上左尾极端损失相对更突出。", "视策略而定", "本地日线"),
    # 流动性
    _f("turnover", "换手率", "流动性因子", "Turnover = Volume / FreeFloatShares", "成交股数相对自由流通股本的比例，衡量股票换手活跃程度。", data="本地日线或成交量 + 自由流通股本", executable="turnover"),
    _f("turnover20", "20日平均换手率", "流动性因子", "Turnover_20 = Avg(Turnover,20)", "过去 20 个交易日换手率均值，降低单日异常成交的影响。", data="本地日线", executable="SMA(turnover,20)"),
    _f("turnover_cv20", "换手率变异系数", "流动性因子", "TurnoverCV = Std(Turnover,20) / Avg(Turnover,20)", "换手率波动相对其均值的比例，用于识别交易活跃度是否稳定。", "越小越稳定", "本地日线", "STD(turnover,20)/SMA(turnover,20)"),
    _f("amihud20", "Amihud 非流动性", "流动性因子", "ILLIQ_20 = Avg(|r| / Amount,20)", "单位成交额引起的绝对价格变动。数值越大，表示少量资金更容易推动价格，流动性越弱。", "越小越好", "本地日线", "SUM(ABS(returns)/MAX(amount,1),20)/20", "成交额单位必须在全市场保持一致。"),
    _f("amount20", "20日成交额因子", "流动性因子", "AmountFactor = ln(Avg(Amount,20))", "过去 20 日平均成交额取对数，衡量实际资金容量。应结合市值中性化，避免退化成规模因子。", data="本地日线", executable="LOG(SMA(amount,20))"),
    _f("bid_ask_spread", "买卖价差", "流动性因子", "Spread = (Ask1 - Bid1) / MidPrice", "最优卖价与买价的相对差，越小表示即时交易摩擦越低。", "越小越好", "Level-2/盘口快照", caveat="日线无法真实恢复历史买卖价差。"),
    # 资金与订单流
    _f("main_net", "主力净流入", "资金与订单流", "MainNet = BuyLarge + BuyXLarge - SellLarge - SellXLarge", "按数据商大单/超大单分类统计的净主动买入额，用于描述大额成交方向。", data="资金流逐笔或数据商历史快照", caveat="大单不等于机构，不同数据商阈值不可混用。"),
    _f("main_net_ratio", "主力净流入率", "资金与订单流", "MainNetRatio = MainNet / Amount", "将主力净流入按当日成交额标准化，便于跨市值和跨交易日比较。", data="资金流 + 成交额"),
    _f("main_net20", "20日主力净流入率", "资金与订单流", "MainNet_20 = ΣMainNet / ΣAmount", "在 20 日窗口内汇总主力净流入并除以成交额汇总，比单日值更稳定。", data="连续历史资金流 + 成交额"),
    _f("large_participation", "大单参与率", "资金与订单流", "LargeParticipation = (Buy/Sell Large + XLarge) / Amount", "大单和超大单成交额占总成交额的比例，描述大额交易参与程度，不区分方向。", data="资金流逐笔或数据商快照"),
    _f("order_imbalance", "主动订单流失衡", "资金与订单流", "OFI = (AggressiveBuy - AggressiveSell) / Amount", "主动买入与主动卖出金额之差相对成交额的比例，刻画短期买卖压力。", data="逐笔成交或 Level-2", caveat="普通日线无法真实判断每笔成交的主动方向。"),
    # 筹码与持仓
    _f("cr10", "前十大流通股东集中度 CR10", "筹码与持仓", "CR10 = Σ Top10FloatHoldingShares / FreeFloatShares", "前十大流通股东持股数占自由流通股本的比例，描述披露口径下的持股集中程度。", data="公告日可用股东披露 + 自由流通股本", caveat="只有定期披露时点，不能伪造成每日实时筹码。"),
    _f("institution_holding", "披露机构持仓比例", "筹码与持仓", "InstitutionHolding = InstitutionHoldingShares / FreeFloatShares", "基金、社保、QFII 等披露机构持股占自由流通股本的比例。", data="公告日可用机构持仓披露"),
    _f("average_holding", "户均持股", "筹码与持仓", "AverageHolding = FreeFloatShares / HolderNumber", "自由流通股本除以股东户数，近似表示每户平均持股数量。", data="股东户数披露 + 自由流通股本"),
    _f("holder_change", "股东户数变化率", "筹码与持仓", "HolderChange = HolderNumber_t / HolderNumber_t-1 - 1", "相邻披露期股东户数的变化。户数下降常被用于观察持股集中趋势，但不能等同于主力增仓。", "通常越小越集中", "历史股东户数披露"),
    _f("concentration70", "70%筹码集中度", "筹码与持仓", "Concentration70 = (PriceHigh70 - PriceLow70) / (PriceHigh70 + PriceLow70)", "覆盖约 70%估算持仓成本的价格区间宽度，越小表示成本分布越集中。", "越小越集中", "可验证的筹码成本分布源/模型", caveat="必须展示来源与模型版本；仅凭日线推算不能冒充真实持仓成本。"),
    _f("concentration90", "90%筹码集中度", "筹码与持仓", "Concentration90 = (PriceHigh90 - PriceLow90) / (PriceHigh90 + PriceLow90)", "覆盖约 90%估算持仓成本的价格区间宽度，观察更宽范围的成本集中程度。", "越小越集中", "可验证的筹码成本分布源/模型", caveat="不同平台的筹码衰减与换手假设不同，结果不可直接互换。"),
    # 分析师与情绪
    _f("eps_revision20", "20日 EPS 预测修正", "分析师与情绪", "EPSRevision20 = EPSForecast_t / EPSForecast_t-20 - 1", "一致预期 EPS 在 20 日内的变化，正值表示分析师整体上调盈利预期。", data="历史时点一致预期快照", caveat="不能用当前预测回填历史，否则产生未来函数。"),
    _f("earnings_surprise", "业绩超预期", "分析师与情绪", "Surprise = (ActualEPS - ExpectedEPS) / |ExpectedEPS|", "实际每股收益相对公告前一致预期的偏离，衡量财报是否超出市场预期。", data="公告前一致预期快照 + 实际财报"),
    _f("upgrade_ratio", "评级上调比例", "分析师与情绪", "UpgradeRatio = Upgrades / (Upgrades + Downgrades)", "窗口内评级上调次数占上调与下调总次数的比例，描述卖方观点变化方向。", data="评级历史快照"),
    _f("target_upside", "目标价上涨空间", "分析师与情绪", "TargetUpside = ConsensusTargetPrice / Price - 1", "一致目标价相对当前股价的潜在空间。", data="历史时点目标价一致预期 + 价格", caveat="目标价可能滞后且覆盖股票存在选择偏差。"),
    _f("news_sentiment", "新闻情绪", "分析师与情绪", "NewsSentiment = Σ w_i × Sentiment_i", "将新闻文本情绪按来源、时效和相关度加权汇总，形成公司级情绪分数。", data="带时间戳新闻全文 + 固定版本情绪模型", caveat="必须保存原文标识、模型版本与发布时间。"),
    _f("analyst_count_change", "分析师覆盖变化", "分析师与情绪", "AnalystCountChange = Count_t - Count_t-N", "跟踪覆盖公司的分析师数量变化，反映卖方关注度增减。", data="历史时点分析师覆盖快照"),
)


FACTOR_BY_KEY = {item.key: item for item in FACTOR_CATALOG}
FACTOR_CATEGORIES = tuple(dict.fromkeys(item.category for item in FACTOR_CATALOG))


FACTOR_MODELS: tuple[FactorModelDefinition, ...] = (
    FactorModelDefinition(
        "price_momentum",
        "中期动量模型",
        "本地可回测",
        ("mom20", "mom60", "mom120", "high52"),
        "Score = 0.25·Z(MOM20) + 0.35·Z(MOM60) + 0.25·Z(MOM120) + 0.15·Z(High52)",
        "综合一个月、三个月、半年动量和52周高点位置，偏向趋势持续而非单一窗口。",
        "close > SMA(close,20)",
        "close < SMA(close,20)",
        "0.25*ZSCORE(ROC(close,20),60)+0.35*ZSCORE(ROC(close,60),120)+0.25*ZSCORE(ROC(close,120),180)+0.15*ZSCORE(close/HHV(close,252),60)",
    ),
    FactorModelDefinition(
        "short_reversal",
        "短期反转模型",
        "本地可回测",
        ("rev5", "bollz20", "rsi14", "vol20"),
        "Score = 0.35·Z(REV5) - 0.25·Z(BOLLZ20) - 0.20·Z(RSI14) - 0.20·Z(VOL20)",
        "寻找短期回撤、价格偏离和 RSI 偏低但波动不过度的标的，属于均值回归模型。",
        "close < SMA(close,20)-STD(close,20)",
        "close >= SMA(close,20)",
        "0.35*ZSCORE(-ROC(close,5),60)-0.25*ZSCORE((close-SMA(close,20))/STD(close,20),60)-0.20*ZSCORE(100*SUM(MAX(returns,0),14)/SUM(ABS(returns),14),60)-0.20*ZSCORE(STD(returns,20),60)",
    ),
    FactorModelDefinition(
        "trend_quality",
        "趋势质量模型",
        "本地可回测",
        ("mom20", "macd_hist", "pv_corr20", "vol20"),
        "Score = 0.35·Z(MOM20) + 0.25·Z(MACD) + 0.20·Z(PVCorr) - 0.20·Z(VOL20)",
        "在趋势收益之外加入动能加速度、量价配合与低波约束，降低只追逐高波动涨幅的倾向。",
        "(close>SMA(close,20)) & (EMA(close,12)>EMA(close,26))",
        "(close<SMA(close,20)) | (EMA(close,12)<EMA(close,26))",
        "0.35*ZSCORE(ROC(close,20),60)+0.25*ZSCORE((EMA(close,12)-EMA(close,26))-EMA(EMA(close,12)-EMA(close,26),9),60)+0.20*CORR(returns,DIFF(LOG(MAX(volume,1)),1),20)-0.20*ZSCORE(STD(returns,20),60)",
    ),
    FactorModelDefinition(
        "low_vol_liquidity",
        "低波动与流动性模型",
        "本地可回测",
        ("vol20", "downside_vol20", "amount20", "amihud20"),
        "Score = -0.35·Z(VOL20) - 0.25·Z(DownsideVol) + 0.25·Z(Amount20) - 0.15·Z(Amihud20)",
        "偏好波动和下行风险较低、成交容量较高、价格冲击较小的股票。",
        "amount > SMA(amount,20)*0.5",
        "amount < SMA(amount,20)*0.2",
        "-0.35*ZSCORE(STD(returns,20),60)-0.25*ZSCORE(SQRT(SUM(MIN(returns,0)*MIN(returns,0),20)/20),60)+0.25*ZSCORE(LOG(SMA(amount,20)),60)-0.15*ZSCORE(SUM(ABS(returns)/MAX(amount,1),20)/20,60)",
    ),
    FactorModelDefinition(
        "technical_balanced",
        "技术面均衡多因子",
        "本地可回测",
        ("mom20", "mom60", "bias20", "rsi14", "vol20", "amount20"),
        "Score = 0.25·Momentum + 0.15·Trend + 0.15·RSI + 0.15·Liquidity - 0.15·Volatility - 0.15·Overheat",
        "把趋势、动量、相对强弱、成交容量、低波动和过热惩罚合并，适合作为本地仓库的默认示范模型。",
        "(close>SMA(close,20)) & (amount>SMA(amount,20)*0.5)",
        "close < SMA(close,20)",
        "0.25*ZSCORE(ROC(close,20),60)+0.15*ZSCORE(ROC(close,60),120)+0.15*ZSCORE(100*SUM(MAX(returns,0),14)/SUM(ABS(returns),14),60)+0.15*ZSCORE(LOG(SMA(amount,20)),60)-0.15*ZSCORE(STD(returns,20),60)-0.15*ABS(ZSCORE((close-SMA(close,20))/STD(close,20),60))",
    ),
    FactorModelDefinition(
        "value_quality",
        "价值质量模型",
        "财务数据模型",
        ("ep", "bp", "cfp", "roe", "gpa", "cash_quality", "accrual"),
        "Score = 0.20·Z(EP)+0.15·Z(BP)+0.15·Z(CFP)+0.20·Z(ROE)+0.15·Z(GP/A)+0.10·Z(CashQuality)-0.05·Z(Accrual)",
        "在低估值基础上要求盈利能力和现金质量，避免单纯购买基本面恶化的低估值公司。",
        data_requirement="公告日可用财务快照 + 每日估值截面",
    ),
    FactorModelDefinition(
        "growth_quality",
        "成长质量模型",
        "财务数据模型",
        ("revenue_growth", "profit_growth", "delta_roe", "delta_gm", "cfoa", "leverage"),
        "Score = 0.25·Z(RevGrowth)+0.25·Z(NPGrowth)+0.15·Z(ΔROE)+0.10·Z(ΔGM)+0.15·Z(CFO/A)-0.10·Z(Leverage)",
        "把收入、利润和盈利效率改善与现金质量、杠杆约束结合，筛选更可持续的成长。",
        data_requirement="公告日可用历史季度财务快照",
    ),
    FactorModelDefinition(
        "money_chip",
        "资金与持仓确认模型",
        "专业数据模型",
        ("main_net20", "large_participation", "cr10", "holder_change", "concentration70"),
        "Score = 0.35·Z(MainNet20)+0.15·Z(LargeParticipation)+0.20·Z(CR10)-0.15·Z(HolderChange)-0.15·Z(Concentration70)",
        "用连续资金流和披露持仓集中变化相互确认。只有保存了真实历史快照时才允许回测。",
        data_requirement="历史资金流 + 公告日股东快照 + 可验证筹码源",
    ),
    FactorModelDefinition(
        "a_share_core",
        "A股核心多因子（指南建议）",
        "综合研究模型",
        ("ep", "bp", "sp", "cfp", "dividend_yield", "roe", "roa", "gpa", "cfoa", "accrual", "revenue_growth", "profit_growth", "delta_roe", "rev5", "mom20", "mom60", "mom120", "high52", "vol20", "beta", "max_drawdown", "turnover20", "amihud20", "main_net20", "cr10", "holder_change", "concentration70"),
        "Score = 0.20·Value + 0.20·Quality + 0.15·Growth + 0.20·Momentum + 0.10·Liquidity + 0.15·MoneyFlow",
        "按指南建议的六大类先去极值、标准化并做行业/市值中性化，再按权重合成。应通过 IC、分组单调性、换手和成本进行样本外验证。",
        data_requirement="完整时点财务、估值、资金流、股东与日线仓库",
    ),
    FactorModelDefinition(
        "fama_french_5",
        "Fama-French 五因子",
        "因子收益模型",
        ("smb", "hml", "rmw", "cma"),
        "R_i-R_f = α + β_MKT·MKT + β_SMB·SMB + β_HML·HML + β_RMW·RMW + β_CMA·CMA + ε",
        "用市场、规模、价值、盈利和投资五个组合收益解释资产超额收益。该模型用于风险归因，不等同于把五个单股原始指标直接相加。",
        data_requirement="每日因子组合收益 + 无风险收益 + 标的收益",
    ),
)


MODEL_BY_KEY = {item.key: item for item in FACTOR_MODELS}


def factor_categories() -> tuple[str, ...]:
    return FACTOR_CATEGORIES


def factors_for_category(category: str) -> list[FactorDefinition]:
    return [item for item in FACTOR_CATALOG if item.category == category]


def model_backtest_template(model_key: str) -> tuple[str, str, str, str]:
    model = MODEL_BY_KEY[model_key]
    if not model.executable:
        raise ValueError(
            f"“{model.name}”需要{model.data_requirement}；当前本地仓库尚不足以进行无编造回测"
        )
    return model.name, model.entry_formula, model.exit_formula, model.score_formula
