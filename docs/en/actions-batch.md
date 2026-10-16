# GitHub Actions batch reports

The existing daily workflow retains its weekday 10:00 UTC schedule and original stock/market entry points. It adds bounded batch modes, not a persistent server or broker trading connection.

| Mode | Purpose |
| --- | --- |
| `full` | Original stock + market report, then hotspots + one market-wide screening run by default |
| `stocks-only`, `market-only` | Original entry points |
| `screening` | A-share candidates using one of ten existing strategies; ten candidates by default |
| `hotspots` | Refresh sector/theme observations, preserving stale/cache/source diagnostics |
| `strategy` | Existing Agent skills on watchlist or one-run stock overrides |
| `backtest` | Evaluate historical reports and render actual sample/accuracy statistics |
| `etf-rotation` | Existing ETF signal, rules backtest and parameter comparisons |
| `data-check` | Fetch data without LLM calls, then inspect notification configuration without sending |

In **Actions → 每日股票分析 → Run workflow**, `stocks` overrides only this run (up to 20 codes); it does not modify repository variables or change screening/ETF/backtest universes. `max_results` is 1–10, not an API-call ceiling. `screening_strategy` selects one strategy, not all of them. `agent_skills` accepts built-in skill IDs or `all`, used only in `strategy` mode. `notify=false` disables pushes; `extras=false` keeps `full` to its original combination. A-share screening/hotspots skip closed days unless `force_run=true`. Other modes retain their existing trading-day behavior.

Scheduled full reports add hotspots and balanced screening, not every Agent skill or long ETF backtest. The original report and one supplemental digest can arrive separately. Ordinary reports explicitly clear Agent skills to avoid the core pipeline automatically enabling Agent mode. Model/relay tool compatibility still requires online validation.

Repository variables:

- `ACTIONS_DAILY_EXTRAS=true`, `ACTIONS_SCREENING_STRATEGY=balanced_alpha`, `ACTIONS_MAX_RESULTS=10`: scheduled extras, strategy and candidate count. Shortfalls are reported without padding or duplicate stocks.
- `LLM_TIMEOUT_SEC=240`, `LLM_MAX_TOKENS=6144`: Actions screening request timeout and output budget. Timed-out paid calls are not automatically retried; factor fallback remains explicit.
- `LLM_RANKING_PROMPT_MAX_CHARS=64000`: Actions input character budget covering all 12 researched candidates; non-Actions defaults to 24000. Truncation remains explicit.
- `SNAPSHOT_SOURCE_PRIORITY=em_datacenter,sina,efinance,akshare_em`: prefer the data-center source verified in hosted runs while retaining fallbacks. A configured Tushare token prepends tushare unless this variable explicitly overrides the order.
- Intraday price plans exclude today's unfinished bar and require the latest completed trading session; invalid, stale or missing completed bars still produce no plan.
- `ACTIONS_TASK_TIMEOUT_SECONDS=900` (60–1800): per-task budget; hotspots are capped at 180 seconds. `ANALYSIS_TIMEOUT_MINUTES` defaults to 60 for the entire job, including setup; existing overrides still apply.
- `AGENT_SKILLS=bull_trend,shrink_pullback`, `AGENT_MAX_STEPS=6`, `AGENT_ARCH=single`: manual Agent defaults. Step limits are not token/cost guarantees. Advanced multi mode needs separate compatibility/cost checks.
- `BACKTEST_ENABLED=true`, `BACKTEST_EVAL_WINDOW_DAYS=10`, `BACKTEST_MIN_AGE_DAYS=14`, and existing `ETF_ROTATION_*` variables reuse the application's configuration.
- `ACTIONS_PERSIST_HISTORY`: **off unless explicitly set to `true`**.

A failed task does not prevent remaining extras from being attempted. New empty/degraded screening, stale hotspots, backtest errors and ETF warnings produce a non-successful workflow while preserving available reports. Original CLI modes retain their own exit-code semantics: completion does not prove every data source or module succeeded.

`reports/actions_summary.md` and `actions_status.json` record results; per-mode Markdown files preserve supplemental reports. The Actions Summary also displays the digest. Existing report/log artifacts retain their 30-day policy. Supplemental notification failure fails the job without discarding reports. SMTP acceptance is not proof of inbox delivery.

## Optional history and privacy

The job uses Asia/Shanghai. The weekday 18:00 schedule is a planned start, not a delivery guarantee: GitHub can delay scheduled runs. Reports distinguish analysis start, generation, quote timestamps and retrieval times. Supplemental emails have a distinct subject and a timezone-aware Date header.

News/events cover up to 12 candidates before model ranking and all selected candidates afterwards, retaining the API's default research limit for other callers. Per-stock details expose selection provenance, quotes/changes/turnover, daily averages/support/resistance, dated news and original links, inferred catalysts/risks/invalidation conditions. Items older than seven days or with unknown publication dates are labelled accordingly. Correlation between news and price movement is not presented as proven causation.

Price levels are conditional technical scenarios over 5–20 trading sessions, not valuation forecasts: require at least 21 valid completed OHLC bars ending on the expected completed session. Assume entry at the last close; stop=max(20-session low, entry−2×ATR20), R=entry−stop, take-profit=entry+2R, target=entry+3R. Missing/stale/invalid bars or invalid risk intervals suppress levels and mark partial output. Entry changes, corporate actions or major news require recalculation. Complete research coverage counts alone are informational; actual data/model failures still mark degradation. Hotspot tables display obtained sector returns without an empty event column.

Treat public-repository Actions logs, artifacts and caches as publicly readable. Do not add account information, holdings or credentials to them. History persistence is opt-in because the database can contain analysis context.

When enabled, the workflow restores a same-branch SQLite snapshot and saves a consistent backup after the run. SQLite's backup API includes committed WAL data and converts the snapshot to a standalone database with an integrity check. Only `data/actions-state/stock_analysis.db` is cached: no `.env`, token caches or entire data directory. Jobs remain serialized; cache keys include branch/run/attempt. Cache eviction is possible; this is not permanent backup. Disabling the variable does not delete old caches; remove those separately through GitHub Caches.

A new deployment has no mature historical samples. The report explicitly states this instead of manufacturing an accuracy rate. Historical evaluation is not a future performance guarantee.

Existing model, QQ email, Bocha and optional quote-provider secret mappings remain in place. Saved credentials are not proof of successful live calls. Optional paid feeds may improve coverage or availability but do not guarantee prediction accuracy. Broker imports need separate authorization/connectivity; persistent Web, chat and monitoring services require another deployment, not a hosted ephemeral runner or public tunnel.

## Verification and rollback

Run the offline tests and `py_compile` documented in `docs/actions-batch.md`. They exercise orchestration, service-call contracts, input safety, notifications, timeouts and WAL snapshots without paid calls, but do not validate actual models/news/quotes/SMTP. After publishing, start with `data-check`, then small screening/strategy runs.

Quick rollback: set `ACTIONS_DAILY_EXTRAS=false` and retain the original three modes. Full rollback restores the previous workflow and removes the added script; secrets, watchlist and cron stay unchanged. Remove stored caches separately if needed. No new PR-related trigger or automatic trading is enabled.
