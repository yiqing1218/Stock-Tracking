# 本地历史数据仓库实施说明

本说明对应《Local History Data Warehouse》阶段一、二、三。所有迁移均使用 `CREATE TABLE IF NOT EXISTS` 和增量字段，不删除原自选、公式、消息或提醒表。

## 阶段一：历史日线仓库

- `securities` 保存股票、ETF、指数目录和数据来源。
- `daily_bars` 以“证券 + 交易日 + 复权方式”为唯一键，保存 OHLCV、成交额、换手率、涨跌幅、来源、抓取时间、质量标记和临时K线标记。
- `sync_jobs`、`sync_job_failures`、`sync_state` 保存任务进度、失败、断点和取消状态。
- `data_quality_issues` 保存可复核的 OHLC 关系错误、负成交值和异常跳变；不自动篡改原始记录。
- 桌面“数据导出”页和 `astock-sync` 命令行共用 `SyncService`。下载最多4并发，SQLite写入采用 WAL 和批量 UPSERT。
- 旧 `data/cache/history_*.csv` 可一键导入；失败文件跳过并保留原文件。
- CSV 导出按单只证券读取、立即写入，避免在内存中拼接全市场数据。

## 阶段二：本地条件扫描

- 条件荐股只调用 `HistoricalStore.get_bars`，仓库为空时直接提示，不执行逐股网络请求。
- 全市场按128只分批，单股最多保留620个交易日，只计算选中的1至5项指标。
- `scan_definitions`、`scan_runs`、`scan_results` 保存筛选定义、运行历史、触发日、触发价和指标原始值。
- `dynamic_groups`、`dynamic_group_members` 保存可刷新的动态分组；结果也可复制成普通自选分组或导出CSV。
- 固定日期扫描通过 `get_bars(..., end=目标日期)` 截断，公式不会看到未来数据。
- 行业、概念、客户集中度等没有可靠字段时保留空值，不以名称或价格猜测。

## 阶段三：统一提醒引擎

- `alert_rules`、`alert_rule_targets`、`alert_states`、`alert_events` 和 `notification_settings` 保存规则、目标、跨重启状态、事件快照和通知设置。
- 支持高于、低于、向上穿越、向下穿越、进入区间和离开区间；使用边沿触发、冷却和事件键去重。
- 行情由消息中心一次批量刷新，再对规则求值；历史条件复用本地仓库，不逐规则下载完整历史。
- 只在北京时间交易时段检查。网络错误或缺失报价不会更新成触发状态。
- Windows 关闭主窗口后进入托盘，提醒继续运行；托盘菜单可恢复或真正退出。
- 涨跌停只按交易板块和 ST 名称做理论阈值提醒，不声称拥有封单队列或 Level-2 数据。

## 数据库备份、迁移与回滚

首次升级前可在程序完全退出后复制：

```powershell
Copy-Item .\data\astock_monitor.db ".\data\astock_monitor.$(Get-Date -Format yyyyMMdd-HHmmss).bak"
```

新增表不影响旧版本读取原表。若需要回滚，退出程序后将备份复制回 `data/astock_monitor.db`；不要只复制 `-wal` 或 `-shm` 文件。

## 磁盘估算

SQLite 单条日K含索引通常需要约 180–350 字节，取决于文本来源和索引页填充。5000只证券、每只约3000个交易日、单一复权口径，约需 3–6 GB；三种复权口径会接近三倍。建议先同步自选，再按需要扩展全市场。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m astock_monitor.sync_cli status
.\.venv\Scripts\python.exe -m astock_monitor.sync_cli validate
```

公开接口会限流或变更字段。正式交易用途应改用有授权、带交易日历和版本追溯的行情源，并在接入层保留相同仓库接口。
