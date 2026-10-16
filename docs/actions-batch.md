# GitHub Actions 批量报告功能

本页对应 `.github/workflows/00-daily-analysis.yml`。这是临时 runner 上的批量入口，不是常驻 Web 服务，也不会连接券商下单。

## 运行方式

在 GitHub 仓库的 **Actions → 每日股票分析 → Run workflow** 选择模式：

| 模式 | 内容 | 模型调用 | 默认推送 |
| --- | --- | --- | --- |
| `full` | 原有自选股分析、大盘复盘；默认追加热点与全市场候选股 | 有，新增选股也可能消耗模型/搜索额度 | 原有报告 + 一份附加汇总 |
| `stocks-only` | 原有自选股分析 | 有 | 原有报告 |
| `market-only` | 原有大盘复盘 | 有 | 原有报告 |
| `screening` | 从 A 股市场筛选候选，默认均衡策略、10 只 | 有；失败时可能退化为因子排序 | 附加汇总 |
| `hotspots` | 刷新热点板块，展示数据源、缓存/过期状态 | 本入口不主动调用 LLM | 附加汇总 |
| `strategy` | 对自选股或本次股票运行内置 Agent 技能 | 有，可能多轮、成本更高 | 原有报告 |
| `backtest` | 复用历史报告回测服务，输出样本、准确率和模拟收益统计 | 本入口不主动调用 LLM | 附加汇总 |
| `etf-rotation` | 原有 ETF 轮动信号、规则回测和参数对比 | 本入口不主动调用 LLM | 附加汇总 |
| `data-check` | 仅获取数据，再检查通知配置；不实际发邮件 | 不调用 LLM | 永不发送 |

其他输入：

- `stocks`：本次股票代码，逗号分隔，最多 20 只。留空保留原 `STOCK_LIST`，不会修改仓库配置。只影响自选股/策略/数据检查等股票分析入口，不改变全市场候选、热点、ETF 池或全局历史回测范围。
- `screening_strategy`：10 个内置策略任选其一；并非同时运行所有策略。
- `max_results`：1–10 只，默认 10 只。这只是输出数量，不是模型请求、搜索请求或数据拉取次数的硬上限。
- `agent_skills`：逗号分隔的技能 ID，见 `strategies/*.yaml`；默认 `bull_trend,shrink_pullback`。仅 `strategy` 模式生效。`all` 可用于手动研究，但不建议日常运行。未知 ID 会在访问网络前报错。
- `extras`：仅影响 `full`，关闭后恢复原“股票 + 大盘”组合。
- `notify`：关闭后只保存报告，不推送；原有通知配置不被删除。
- `force_run`：跳过交易日检查。热点与全市场选股默认在 A 股休市日跳过；原有股票/大盘保留自己的交易日判断。回测与 ETF 历史研究不限制交易日。

Agent 模式复用项目现有实现，能否完成工具调用取决于所配置模型/中转站能力；仅有普通文本分析成功记录，不能证明 Agent 工具调用兼容。

## 自动运行与限额

- 原定时不变：周一至周五 UTC 10:00，即北京时间 18:00；Actions 可能延迟启动。
- 定时 `full` 默认增加一次热点刷新和一次均衡选股，候选 10 只；不会自动改自选股，不会自动运行全部技能或 ETF 长期回测。
- 每项任务默认超时 900 秒，热点最多 180 秒；工作流默认总超时从 30 分钟改为 60 分钟。原有 `ANALYSIS_TIMEOUT_MINUTES` 若已设置仍优先，较短值可能导致任务来不及完成。
- 主分析失败后仍尝试附加任务，保留已经完成的报告。新增选股/热点的空结果、缓存过期、LLM 排名失败、回测错误或 ETF 数据警告会标为部分/降级，工作流返回失败，避免把缺失数据当成成功结论。
- 原有 `main.py` 分析入口继续沿用自己的退出码和报告语义，流程“完成”不保证每个数据源或模块都成功。请查看报告与日志。

可在 **Settings → Secrets and variables → Actions → Variables** 设置：

| Repository variable | 默认 | 用途 |
| --- | --- | --- |
| `ACTIONS_DAILY_EXTRAS` | `true` | 设 `false` 关闭定时附加热点/选股，立即恢复原组合 |
| `ACTIONS_SCREENING_STRATEGY` | `balanced_alpha` | 定时选股策略；手动选择优先 |
| `ACTIONS_MAX_RESULTS` | `10` | 定时候选数，范围 1–10；不足时明确标记，不重复补数 |
| `LLM_TIMEOUT_SEC` / `LLM_MAX_TOKENS` | `240` / `6144` | Actions 选股模型等待秒数 / 输出预算；超时保留因子降级结果，不自动重试付费请求 |
| `ACTIONS_TASK_TIMEOUT_SECONDS` | `900` | 单项任务超时，范围 60–1800 秒；热点另有 180 秒上限 |
| `AGENT_SKILLS` | `bull_trend,shrink_pullback` | 手动策略模式默认技能；不会自动启用普通日报的 Agent |
| `AGENT_MAX_STEPS` | `6` | Agent 步数预算；不是 token/费用上限 |
| `AGENT_ARCH` | `single` | 复用项目 Agent 架构配置；高级 multi 模式需自行验证模型兼容性与开销 |
| `BACKTEST_ENABLED` / `BACKTEST_EVAL_WINDOW_DAYS` / `BACKTEST_MIN_AGE_DAYS` | `true` / `10` / `14` | 原有自动回测及样本成熟窗口配置 |
| `ETF_ROTATION_*` | 项目默认值 | ETF 池、安全资产、回看、调仓、成本、回测年数等；见 `.env.example` |
| `ANALYSIS_TIMEOUT_MINUTES` | `60` | 整个任务的超时（包括安装依赖） |
| `ACTIONS_PERSIST_HISTORY` | 未设置/关闭 | 显式同意后才开启历史 SQLite 快照缓存，见下节 |

工作流输入通过环境变量传给 Python，股票代码通过参数数组传给原 CLI，不会作为 shell 指令执行。新增入口不打印密钥，不新增付费订阅，也没有修改 Secrets 的行为。

## 报告与邮件

- 工作流使用 `Asia/Shanghai`；计划工作日18:00启动。GitHub定时任务可能延迟，不能保证18:00送达；邮件列出分析开始、生成时间，行情时间单独列出，不能把获取时间当作行情时间。
- 默认请求10只候选；参与模型排序的最多12只候选补充新闻/事件，入选股票均补充材料。模型超时、数据源异常、候选不足或价格测算缺失仍标记部分结果；完整的新闻覆盖计数本身不会触发报错。
- 逐股展示入选理由及其来源、报价/涨跌幅/成交额/量比/换手率、行情时间与来源、日线均线/支撑压力、新闻与公告的日期/来源/原文链接，以及模型催化、风险和失效条件。近7日与旧资料、未核验日期区分展示；不把新闻与涨跌的同时发生写成已证明的因果关系。
- 目标价、止盈价、止损价为条件式技术测算：至少21条有效完整日线，最后日期必须匹配应有完整交易日；以末日收盘价为入场假设，止损=max(20日低点, 入场−2×ATR20)，R=入场−止损，止盈=入场+2R，技术目标=入场+3R。观察期5–20个交易日。过期/缺失/无效OHLC或无有效风险区间时不填价格，实际入场变化或重大新闻后需重算。
- 附加邮件主题为“选股与热点补充报告”，与原有股票日报区分；邮件增加带时区的Date头。热点表仅展示实际取得的板块行情，不再放置一整列缺失的事件摘要。
- 原有日报、QQ 邮箱和搜索 Secret 继续使用；`BOCHA_API_KEYS`、行情服务的 Secret 映射保留。
- `full` 的热点/选股合并到一份附加汇总，可能比原日报晚收到。并非把它们嵌入原日报模板；通知开关关闭时都不发送。
- `reports/actions_summary.md`：运行概览与新增模块报告；`reports/actions_status.json`：各项退出状态。
- `reports/actions_screening.md`、`actions_hotspots.md`、`actions_backtest.md`、`actions_etf-rotation.md`：对应模式产物。
- Actions 页面 Summary 也显示附加汇总；报告和日志按原规则作为 artifact 保存 30 天。
- 补充报告通知不可用/发送失败时，工作流报错，文件仍保存。主报告通知继续沿用原项目实现。SMTP 提交成功不等于收件箱实际收到。

## 历史回测与隐私

**公开仓库的 Actions 日志、报告、缓存应视为可被他人读取。不要往缓存或报告写持仓、账户、密钥或其他隐私。**

默认不增加跨运行的历史留存。在明确接受公开历史风险后，可把 Repository variable `ACTIONS_PERSIST_HISTORY` 设为 `true`：

1. 每次运行恢复同一分支最近的 `data/actions-state/stock_analysis.db`。
2. 任务结束后通过 SQLite backup API 制作一致快照，包含 WAL 中已提交记录，并转换成独立、通过完整性检查的数据库。
3. 缓存仅包含这一数据库文件，不缓存 `.env`、认证令牌缓存、整个 `data/` 或券商连接配置。数据库自身可能保存分析上下文，因此仍有公开历史风险。
4. 历史库可用于后续报告事后评估；新部署没有成熟样本时只显示样本不足，不虚构 0%/100% 准确率。
5. GitHub cache 可能被清理/淘汰，不是永久备份；缓存键包含分支、run ID 和重试次数，同分支任务继续串行运行。

关闭变量会停止恢复/保存，**不会自动删除已保存的历史缓存**；需要在 GitHub Actions 的 Caches 页面自行删除。工作流只在定时/手动事件触发，不新增 `pull_request_target` 或其他不可信代码带 Secrets 的触发入口。

## 数据源与边界

- 可继续使用已有免费备用行情源；增加模型数量不能补回缺失的行情。建议先用 `data-check` 看实际可用性与日志。
- 已有博查用于新闻检索；保存 Secret 不代表请求一定成功，仍需在线运行验证额度、网络与返回内容。
- `TUSHARE_TOKEN`、`TICKFLOW_API_KEY` 等可选 Secret 已有映射；各供应商的数据权限不同。是否值得配置应根据缺失字段决定，不承诺付费后提高预测准确率。
- 长桥、券商持仓导入需要独立授权/网络环境；本扩展不替用户开通或保存持仓。
- Web 控制台、多轮聊天机器人、全天事件监控、实时告警需要常驻部署，不在 GitHub-hosted runner 中启动常驻服务或公开隧道。
- 所有结果是研究材料，不是自动交易指令或收益承诺。

## 验证、发布与回退

离线测试：

```bash
python -m pytest -q tests/test_actions_report.py tests/test_daily_analysis_workflow_llm_env.py tests/test_ci_workflow_contract.py
python -m py_compile scripts/actions_report.py tests/test_actions_report.py
```

这些测试覆盖调度/输入边界、通知开关、服务调用契约、降级、超时清理和 SQLite WAL 快照，不调用付费服务，不能替代真实模型、行情、新闻、ETF 数据与 SMTP 验收。正式发布后优先手动运行 `data-check`，再以少量候选验证 `screening`、`strategy`，避免直接重跑全部耗费额度。

快速回退：设 `ACTIONS_DAILY_EXTRAS=false`，继续使用原有三种模式；需要完全撤回时恢复本次变更前的工作流并移除新增脚本。保持 Secrets、原自选股及 cron 不变。关闭历史缓存后如需清理，另行删除 GitHub Caches。

English guide: `docs/en/actions-batch.md`.
