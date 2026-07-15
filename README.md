# 澄鉴 A股监看

一个本地 Windows 桌面行情终端，只覆盖中国 A 股、境内 ETF 和 A 股指数。应用包含两个核心页面：

1. 自选页：按代码或名称搜索全部 A 股、ETF、指数，加入或移除自选，查看实时快照。
2. 详情页：K线与量能、指定日期分时、完整技术指标、资金/筹码/股东、公司财务、实时资讯和自定义指标。

数据通过 AkShare 访问公开行情源。自选和公式只保存在本机 `data/astock_monitor.db`，不需要账号、API Key 或云端服务。

## 直接运行

本机已经创建好 `.venv` 时，双击：

```text
启动A股监看.bat
```

也可以在 PowerShell 中执行：

```powershell
.\run.ps1
```

首次在其他 Windows 电脑上运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
```

建议使用 Python 3.12、Windows 10/11 和正常可访问公开财经网站的网络环境。

## 页面一：自选

- 搜索支持纯代码、`SH/SZ/BJ/CSI + 代码`、完整名称和部分名称，范围严格限定为 A 股、境内 ETF、A 股指数。
- 证券目录优先从本地完整缓存载入，避免上游超时后只能搜到少数内置证券。
- 支持自选分组；可选择加入目标分组，并通过“分组管理”新建、重命名或删除分组。
- 双击行进入详情；右键菜单提供打开详情、上移、下移、移到分组和移除。
- 展示最新价、涨跌幅、成交额、换手率、量比、PE、PB、总市值等公开字段。
- 表格直接显示本次行情实际采用的数据源，不再显示“详情/移除”按钮。
- 行情每 60 秒后台刷新，网络请求不阻塞界面；首次完整证券列表同步后使用 24 小时缓存。

## 页面二：详情

### 行情走势

- 红涨绿跌的日K、成交量、MA5/MA20、布林带和 MACD。
- 3月、6月、1年、3年、全部区间；鼠标滚轮缩放K线。
- 日线自动合并北京时间当日实时报价；右侧可切换前复权、不复权和后复权。
- 双击任意日K会直接进入该交易日的1分钟分时；右侧同步展示筹码成本分布图。
- 趋势/震荡状态、综合多维评分、历史波动率、回撤与六维状态摘要。

### 历史分时

- 可用日历选择某一天，并切换 1、5、15、30、60 分钟周期，默认1分钟。
- 北京时间当日采用实时分时，20秒短缓存后重新请求；历史日期使用长期本地缓存。
- 分时图显示日内 K 线、成交量和时间轴；非交易日或接口范围外会明确提示。
- 公开接口的 1 分钟数据通常只保留最近 5 个交易日，较长周期的可用日期一般更长。

### 全部指标

当前目录约 75 个指标，可按趋势、动量、波动、量能、情绪、风险筛选：

- 趋势：MA/EMA/WMA、MACD、DMI/ADX、Aroon、SAR、Supertrend、一目均衡、BBI、DMA、线性斜率。
- 动量：RSI、KDJ、Stochastic、Williams %R、ROC、MOM、CCI、CMO、TRIX、PPO、Ultimate Oscillator。
- 波动：BOLL、ATR/NATR、历史波动率、Keltner、Donchian、Chaikin Volatility、Ulcer Index。
- 量能：OBV、MFI、CMF、A/D、Chaikin Oscillator、PVT、Force Index、EMV、量比、VWAP。
- A股常见情绪：BIAS、PSY、AR、BR、CR。
- 风险：多周期收益、滚动夏普、历史 VaR、当前回撤、偏度、峰度。
- K线形态：十字星、锤头、射击之星、看涨/看跌吞没。

### 资金、筹码与公司

原“资金与筹码”和“公司与财务”已合并，在同一页面通过二级导航切换：主力资金、筹码分布、主要股东、企业概况、主营业务、财务信息。

- 主力资金流：公开数据源按超大单、大单、中单、小单统计的资金净额与占比。
- 筹码分布：获利比例、平均成本、70%/90%成本区间与集中度。
- 主要股东：最新可用定期报告的股东名称、数量、比例、性质和披露日期。

“主力资金流”是成交单大小的统计口径，不等于机构真实持仓；“主要股东”是定期披露口径，也不代表实时仓位。应用在页面中明确分开显示。

- 企业概况：上市时间、行业、总股本、流通股、公司名称、注册地址等可取得字段。
- 主营业务：主营范围、产品与经营介绍等公开资料。
- 财务信息：按报告期展示主要财务指标，并绘制营业收入、净利润和同比增速趋势；同花顺源失败时回退新浪财务分析接口。
- ETF 和指数没有单一上市公司的财务报表，页面会显示“不适用”。

### 实时资讯

- 自动汇总东方财富个股新闻与 Bing 联网新闻搜索结果，转换成“时间、来源、标题、摘要”条目。
- 自动去重并保留 10 分钟缓存；双击条目可打开原文。

## 自定义指标

公式解析器使用 AST 白名单，不调用 Python `eval`，不能访问文件、网络、模块或对象属性。

基础变量：

```text
open, high, low, close, volume, amount, turnover,
pct_change, returns, typical, hl2, ohlc4
```

可用函数：

```text
SMA(x,n) EMA(x,n) WMA(x,n) SUM(x,n) STD(x,n)
HHV(x,n) LLV(x,n) REF(x,n) DIFF(x,n) ROC(x,n)
ZSCORE(x,n) TS_RANK(x,n) CORR(x,y,n)
IF(condition,a,b) CROSS(a,b) MAX(a,b) MIN(a,b)
ABS(x) LOG(x) SQRT(x) CLIP(x,min,max)
```

示例：

```text
EMA(close, 12) - EMA(close, 26)
(close / SMA(close, 20) - 1) * 100
ZSCORE(returns, 20) + ZSCORE(volume, 20)
IF(CROSS(SMA(close, 5), SMA(close, 20)), 1, 0)
```

公式校验、保存和绘图都在详情页完成。

## 数据与计算口径

- 实时行情按“东方财富直连 → 腾讯行情直连 → 新浪行情直连 → 本地历史缓存”自动回退。
- 日线按品种使用下列 AkShare 接口自动回退：
  - 股票：`stock_zh_a_hist` → `stock_zh_a_hist_tx` → `stock_zh_a_daily`
  - ETF：`fund_etf_hist_em` → `fund_etf_hist_sina`
  - 指数：`stock_zh_index_daily_em` → `stock_zh_index_daily_tx` → `stock_zh_index_daily` → `index_zh_a_hist`
- 分时接口：股票使用 `stock_zh_a_hist_min_em` / `stock_zh_a_minute`；ETF 使用 `fund_etf_hist_min_em` / `stock_zh_a_minute`；指数使用 `index_zh_a_hist_min_em` / `stock_zh_a_minute`。
- 证券目录：`stock_info_a_code_name`、`fund_etf_spot_em`、`stock_zh_index_spot_em`。
- 公司和财务：`stock_ipo_info`、`stock_individual_info_em`、`stock_profile_cninfo`、`stock_individual_basic_info_xq`、`stock_zyjs_ths`、`stock_financial_abstract_ths`、`stock_financial_analysis_indicator`。
- 资金和持仓：`stock_individual_fund_flow`、`stock_cyq_em`、`stock_main_stock_holder`。
- 资讯：`stock_news_em` 加 Bing 新闻 RSS 搜索；不需要 API Key。
- AkShare 接口定义与字段以 [AkShare 股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html) 和 [ETF 分时文档](https://akshare.akfamily.xyz/data/fund/fund_public.html) 为准。
- 指标分类参考 [TA-Lib 官方函数目录](https://ta-lib.org/functions/)；实际公式由本项目用 pandas/numpy 实现。
- 滚动均值、标准差和指数加权使用 [pandas Window API](https://pandas.pydata.org/pandas-docs/stable/reference/window.html)。
- 日线默认前复权；指标基于 OHLCV、成交额、换手率等公开字段。
- 所有“今天”、交易状态、缓存年龄和界面时间均统一按 `Asia/Shanghai` 北京时间计算，不受 Windows 当前时区影响。
- 指数和 ETF 不显示个股股东/筹码披露，避免制造不存在的数据。
- 网络失败时优先读取最近一次成功缓存，并在界面显示提示。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试覆盖指标边界、综合状态、自定义公式安全限制、搜索匹配、分组排序、本地持久化、当日日K合成和分时重复加载。

## 项目结构

```text
src/astock_monitor/
  app.py               应用入口
  main_window.py       两页导航
  watchlist_page.py    搜索、自选、实时快照
  detail_page.py       详情页各功能区
  chart_widget.py      自绘K线/成交量/指标图
  data_provider.py     多源接口、回退与缓存
  indicators.py        技术指标和状态解释
  formula_engine.py    安全自定义公式
  repository.py        SQLite本地持久化
tests/                 自动测试
```

## 注意

公开行情接口可能延迟、限流、调整字段或临时不可用。本程序是研究与监看工具，不提供交易执行，也不构成投资建议。真实交易前应使用持牌行情源并独立核验数据。
