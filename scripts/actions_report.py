"""Bounded batch entry points for the GitHub Actions deployment."""

from __future__ import annotations

import argparse
from contextlib import closing
import html
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.actions_screening_report import attach_technical_plans, candidate_details, candidate_summary

MODES = (
    "full", "stocks-only", "market-only", "screening", "hotspots",
    "strategy", "backtest", "etf-rotation", "data-check",
)
STRATEGIES = (
    "risk_groups", "balanced_alpha", "blue_chip_income", "capital_heat", "dual_low",
    "low_volatility_quality", "momentum_quality", "oversold_reversal",
    "quality_value", "shrink_pullback", "volume_breakout",
)
REPORT_DIR = Path("reports")
PARTIAL = 2
SKIPPED = 3


def flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return raw == "true"


def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name) or default)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def stock_override() -> list[str]:
    stocks = [s for s in re.split(r"[,，\s]+", os.getenv("ACTIONS_STOCKS", "").strip()) if s]
    if len(stocks) > 20 or any(not re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9.^:_-]{0,24}", s) for s in stocks):
        raise ValueError("ACTIONS_STOCKS: use at most 20 stock codes, separated by commas")
    return stocks


def cell(value) -> str:
    if value is None or value == "":
        return "未提供"
    text = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\|\[\]*_`])", r"\\\1", text)


def diagnostics(data: dict) -> list[str]:
    lines = []
    for key in ("warnings", "source_errors", "degradation", "llm_failure_reason", "message"):
        values = data.get(key) or []
        if not isinstance(values, list):
            values = [values]
        lines.extend(f"- {key}: {cell(value)}" for value in values)
    return lines


def render_screening(data: dict, requested: int | None = None, *, compact: bool = False) -> tuple[str, int]:
    candidates = data.get("candidates") or []
    def actionable(value):
        match = re.fullmatch(r"DSA provider context applied (\d+) of (\d+) candidates", str(value))
        return not match or match[1] != match[2]

    degraded = any(actionable(value) for key in ("warnings", "degradation") for value in data.get(key) or [])
    degraded = degraded or bool(data.get("source_errors") or data.get("llm_failure_reason"))
    degraded = degraded or data.get("ranking_mode") != "llm" or not candidates
    shortfall = requested is not None and len(candidates) < requested
    incomplete = any((item.get("technical_plan") or {}).get("status") != "条件式技术测算"
                     for item in candidates) if requested is not None else False
    incomplete = incomplete or bool((data.get("dsa_enrichment") or {}).get("warnings"))
    degraded = degraded or shortfall or incomplete
    if compact:
        lines = ["# 全市场候选股", "", f"候选 {len(candidates)} 只"
                 + (f" / 请求 {requested} 只" if shortfall else "")
                 + f"；{'部分结果，数据或模型有缺口，详见完整报告' if degraded else '分析完成'}。"]
        for key, label in (("llm_market_view", "市场判断（模型）"), ("llm_portfolio_risk", "组合风险")):
            if data.get(key):
                lines.extend(["", f"{label}：{cell(data[key])}"])
        if not candidates:
            lines.extend(["", "暂无可用候选。"])
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        for index, item in enumerate(candidates, 1):
            lines.extend(candidate_summary(item, index, cell, today))
        lines.extend(["", "价格口径：以所列完整日线收盘价作入场假设，止损取20日低点与入场−2×ATR20中的较高值；"
                      "R=入场−止损，止盈=入场+2R，目标=入场+3R，观察5–20个交易日。实际入场或重大消息变化须重算，跳空止损不保证成交。",
                      "", "仅供研究。理由为模型判断；消息链接仅是检索线索，需核验原文，不能证明涨跌因果；"
                      "行情时间未核验的报价不视为实时价。详细证据与诊断见完整报告。"])
        return "\n".join(lines), PARTIAL if degraded else 0
    lines = [
        "# 全市场候选股", "", "仅供研究，不会下单，也不会自动替换自选股。",
        f"策略：{cell(data.get('strategy'))}；排序：{cell(data.get('ranking_mode'))}；"
        f"快照来源：{cell(data.get('snapshot_source'))}。",
        f"状态：{'降级或无候选，请先检查数据与模型' if degraded else '已生成候选'}。", "",
        "| 代码 | 名称 | 分数（非收益预测） | 筛选理由 | 风险 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in candidates:
        fields = (item.get("code"), item.get("name"), item.get("score"),
                  item.get("reason"), item.get("risk_summary") or item.get("risk_flags"))
        lines.append("| " + " | ".join(cell(v) for v in fields) + " |")
    if not candidates:
        lines.append("\n没有可用候选；不代表市场没有机会。")
    if requested is not None:
        lines.extend(["", f"请求 {requested} 只，实际返回 {len(candidates)} 只。"
                      + ("数量不足：保留筛选标准，不用重复股票或不合格股票补数。" if shortfall else "")])
    for key, label in (("llm_market_view", "模型市场判断"), ("llm_selection_logic", "模型选股逻辑"),
                       ("llm_portfolio_risk", "组合风险"), ("portfolio_concentration_notes", "行业集中度提示")):
        lines.extend(["", f"{label}：{cell(data.get(key))}"])
    lines.extend(["", "以下模型判断属于推断；原始行情和新闻来源列在各股详情中。价格是技术情景值，不是收益预测。"])
    for index, item in enumerate(candidates, 1):
        lines.extend(candidate_details(item, index, cell, datetime.now(ZoneInfo("Asia/Shanghai")).date()))
    lines.extend(["", *diagnostics(data)])
    return "\n".join(lines), PARTIAL if degraded else 0


def render_hotspots(data: dict) -> tuple[str, int]:
    rows = data.get("hotspots") or []
    degraded = bool(data.get("stale") or data.get("cache_used") or data.get("source_errors") or not rows)
    lines = ["# 热点板块观察", "", f"来源：{cell(data.get('provider_used') or data.get('provider'))}。",
             f"使用缓存：{bool(data.get('cache_used'))}；过期：{bool(data.get('stale'))}；"
             f"缓存时间：{cell(data.get('cached_at'))}；备用来源：{bool(data.get('fallback_used'))}。",
             "", "板块涨幅仅是行情线索，未取得事件材料时不能解释为新闻驱动。",
             "", "| 板块/主题 | 涨跌幅（%） |", "| --- | --- |"]
    for row in rows:
        fields = (row.get("topic") or row.get("name"), row.get("change_pct"))
        lines.append("| " + " | ".join(cell(v) for v in fields) + " |")
    if not rows:
        lines.append("\n未取得热点数据，不能解读为市场没有热点。")
    lines.extend(["", *diagnostics(data)])
    return "\n".join(lines), PARTIAL if degraded else 0


def render_recommendations(data: dict, *, compact: bool = False) -> tuple[str, int]:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    lines = ["# 分风险选股报告", "", f"目标 {data['requested']} 只，实际 {len(data['candidates'])} 只；各组独立筛选、股票不重复。",
             "", "高、中风险组取自本次上涨热点的成分股；全报告银行最多2只，每组同一已知行业或热点最多3只。",
             "风险为波动分档：高风险波动较大；中风险控制波动与回撤；低风险须通过低波动、估值和日线质量筛选，不代表保本。"]
    partial = bool(data.get("warnings")) or len(data.get("groups") or []) != 3
    for group in data.get("groups") or []:
        candidates = group["candidates"]
        partial |= render_screening(group, requested=group["requested"])[1] == PARTIAL
        lines.extend(["", f"## {group['title']}（{len(candidates)}/{group['requested']}只）", ""])
        if len(candidates) < group["requested"]:
            lines.append("数量不足：热点、风险、有效价格或分散条件不满足，未用其他风险档或重复股票补数。")
        if compact:
            lines.extend(["", "| 股票 | 热点/行业 | 推荐理由 | 目标购入价 | 止损价 | 止盈价 |",
                          "| --- | --- | --- | --- | --- | --- |"])
            for item in candidates:
                plan = item["technical_plan"]
                fields = [f"{item['name']}（{item['code']}）", item.get("recommendation_topic") or item.get("industry") or "行业待核验",
                          item["recommendation_reason"], f"{plan['entry']:.2f}", f"{plan['stop_loss']:.2f}", f"{plan['take_profit']:.2f}"]
                lines.append("| " + " | ".join(cell(value) for value in fields) + " |")
            dates = sorted({item["technical_plan"]["as_of"] for item in candidates})
            if dates:
                lines.append("\n价格依据：" + cell("、".join(dates)) + "完整日线，单位元。")
            if group.get("llm_failure_reason") or group.get("ranking_mode") != "llm":
                lines.append("\n模型分析未完成，本组采用量价规则筛选；详细诊断见完整报告。")
        else:
            for index, item in enumerate(candidates, 1):
                lines.extend(["", f"推荐理由：{cell(item['recommendation_reason'])}",
                              f"热点归属来源：{cell(item.get('recommendation_source'))}"])
                lines.extend(candidate_details(item, index, cell, today))
            lines.extend(["", *diagnostics(group)])
    if data.get("warnings"):
        lines.extend(["", "数据提醒：部分热点来源不可用，详见完整报告。"] if compact else ["", *diagnostics(data)])
    lines.extend(["", "目标购入价是最新完整日线收盘价的参考入场假设；止损取20日低点与购入价−2×ATR20的较高值；"
                  "止盈=购入价+2×风险距离。观察5–20个交易日，跳空或重大消息变化须重算。",
                  "热点关联来自板块成分关系，不能证明新闻导致涨跌。新闻证据、来源及逐股风险见完整报告。"])
    return "\n".join(lines), PARTIAL if partial else 0


def render_backtest(stats: dict, summary: dict | None) -> tuple[str, int]:
    lines = ["# 历史报告回测", "", "这是历史建议的事后评估，不是未来收益保证。",
             f"本次处理 {stats.get('processed', 0)} 条；完成 {stats.get('completed', 0)} 条；"
             f"样本不足 {stats.get('insufficient', 0)} 条；错误 {stats.get('errors', 0)} 条。"]
    if not summary or not summary.get("completed_count"):
        lines.append("\n尚无足够的成熟样本，不能计算有效胜率或准确率。新部署或未恢复历史数据库时属正常现象。")
    else:
        labels = {"completed_count": "完成样本数", "direction_accuracy_pct": "方向准确率（%）",
                  "win_rate_pct": "胜率（%）", "avg_simulated_return_pct": "平均模拟收益（%）",
                  "computed_at": "统计时间"}
        lines.extend(f"- {label}：{cell(summary.get(key))}" for key, label in labels.items())
    lines.extend(["", *diagnostics(stats)])
    return "\n".join(lines), PARTIAL if stats.get("errors") else 0


def run_service(task: str) -> tuple[str, int]:
    from src.config import get_config

    config = get_config()
    if task in {"screening", "hotspots"}:
        from src.core.trading_calendar import get_open_markets_today
        from src.services.screening_service import ScreeningService
        from src.storage import DatabaseManager

        if not flag("ACTIONS_FORCE_RUN") and config.trading_day_check_enabled and "cn" not in get_open_markets_today():
            return f"# {task}\n\nA 股休市，已跳过；可通过 force_run 手动覆盖。", SKIPPED
        config.screening_enabled = True
        service = ScreeningService(config, db_manager=DatabaseManager.get_instance())
        if task == "hotspots":
            return render_hotspots(service.hotspots(top=12, refresh=True, include_details=False))
        strategy = os.getenv("ACTIONS_SCREENING_STRATEGY") or "risk_groups"
        if strategy not in STRATEGIES:
            raise ValueError("Unsupported screening strategy")
        requested = bounded_int("ACTIONS_MAX_RESULTS", 10, 1, 10)
        os.environ.setdefault("LLM_TIMEOUT_SEC", "240")
        os.environ.setdefault("LLM_MAX_TOKENS", "6144")
        os.environ.setdefault("LLM_RANKING_PROMPT_MAX_CHARS", "64000")
        if strategy == "risk_groups":
            from scripts.actions_risk_screening import build_recommendations

            REPORT_DIR.mkdir(parents=True, exist_ok=True)

            def checkpoint(data):
                (REPORT_DIR / "actions_screening.md").write_text(render_recommendations(data)[0], encoding="utf-8")
                (REPORT_DIR / "actions_screening_email.md").write_text(
                    render_recommendations(data, compact=True)[0], encoding="utf-8")

            data = build_recommendations(service, requested, attach_technical_plans, on_group=checkpoint)
            return render_recommendations(data)
        data = service.screen(strategy=strategy, market="cn", max_results=requested, research_limit=12)
        attach_technical_plans(data)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        compact, _ = render_screening(data, requested=requested, compact=True)
        (REPORT_DIR / "actions_screening_email.md").write_text(compact, encoding="utf-8")
        return render_screening(data, requested=requested)
    if task == "backtest":
        from src.services.backtest_service import BacktestService

        service = BacktestService()
        return render_backtest(service.run_backtest(), service.get_global_summary())
    if task == "etf-rotation":
        from src.services.etf_rotation_service import run_etf_rotation

        report = run_etf_rotation(config, send_notification=False)
        return report.markdown, PARTIAL if report.failed_codes or report.warnings or report.as_of is None else 0
    raise ValueError("Unsupported service task")


def run_command(command: list[str], timeout: int, env: dict) -> int:
    process = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        print("::error::Batch task timed out; remaining tasks will still be attempted", flush=True)
        return 124


def main_command(mode: str, stocks: list[str], notify: bool, force: bool) -> list[str]:
    command = [sys.executable, "main.py"]
    if mode in {"stocks-only", "strategy", "data-check"}:
        command.append("--no-market-review")
    elif mode == "market-only":
        command.append("--market-review")
    if stocks:
        command.extend(["--stocks", ",".join(stocks)])
    if not notify or mode == "data-check":
        command.append("--no-notify")
    if force:
        command.append("--force-run")
    if mode == "data-check":
        command.append("--dry-run")
    return command


def orchestrate() -> int:
    mode = os.getenv("ACTIONS_MODE") or "full"
    if mode not in MODES:
        raise ValueError("Unsupported Actions mode")
    stocks = stock_override()
    notify, force = flag("ACTIONS_NOTIFY", True), flag("ACTIONS_FORCE_RUN")
    timeout = bounded_int("ACTIONS_TASK_TIMEOUT_SECONDS", 1800, 60, 1800)
    bounded_int("ACTIONS_MAX_RESULTS", 10, 1, 10)
    if (os.getenv("ACTIONS_SCREENING_STRATEGY") or "risk_groups") not in STRATEGIES:
        raise ValueError("Unsupported screening strategy")
    if mode == "strategy":
        skills = [s.strip() for s in os.getenv("ACTIONS_AGENT_SKILLS", "").split(",") if s.strip()]
        known = {p.stem for p in Path("strategies").glob("*.yaml")} | {"all"}
        if any(s not in known for s in skills):
            raise ValueError("Unknown Agent skill; see strategies/ for available skill IDs")
    tasks = [mode]
    if mode == "full" and flag("ACTIONS_DAILY_EXTRAS", True):
        tasks.extend(["hotspots", "screening"])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    results, supplements, email_supplements = [], [], []
    for task in tasks:
        env = os.environ.copy()
        env["AGENT_MODE"] = "true" if task == "strategy" else "false"
        env["AGENT_SKILLS"] = os.getenv("ACTIONS_AGENT_SKILLS", "") if task == "strategy" else ""
        for key in ("ETF_ROTATION_POOL", "ETF_ROTATION_SAFE_ASSET"):
            if not env.get(key):
                env.pop(key, None)
        report_path = REPORT_DIR / f"actions_{task}.md"
        email_path = REPORT_DIR / f"actions_{task}_email.md"
        report_path.unlink(missing_ok=True)
        email_path.unlink(missing_ok=True)
        if task in {"full", "stocks-only", "market-only", "strategy", "data-check"}:
            command = main_command(task, stocks, notify, force)
        else:
            command = [sys.executable, "-m", "scripts.actions_report", "--task", task]
        print(f"::group::{task}", flush=True)
        code = run_command(command, min(timeout, 180) if task == "hotspots" else timeout, env)
        if task == "data-check" and code == 0:
            code = run_command([sys.executable, "main.py", "--check-notify"], 60, env)
        print("::endgroup::", flush=True)
        status = {0: "完成", PARTIAL: "降级/部分结果", SKIPPED: "休市跳过"}.get(code, "失败")
        results.append({"task": task, "status": status, "exit_code": code})
        if report_path.exists():
            supplements.append(report_path.read_text(encoding="utf-8"))
            email_supplements.append(email_path.read_text(encoding="utf-8") if email_path.exists() else supplements[-1])
        elif task not in {"full", "stocks-only", "market-only", "strategy", "data-check"}:
            supplements.append(f"# {task}\n\n{status}，退出码 {code}；未产生可用报告，请检查 Actions 日志。")
            email_supplements.append(supplements[-1])
    summary = "# 选股与热点补充报告\n\n" + "\n".join(f"- {r['task']}：{r['status']}（{r['exit_code']}）" for r in results)
    summary += f"\n\n分析开始：{started_at.isoformat()}（北京时间）；触发方式：{cell(os.getenv('GITHUB_EVENT_NAME', '本地/手动'))}。"
    summary += "\n\n定时计划：工作日07:00北京时间；A股休市日跳过。GitHub Actions可能延迟触发，计划时间不等于邮件到达时间；分析开始前还包含排队和安装依赖。"
    summary += "\n\n数据与模型可能降级；完成仅表示入口执行完成，详情以报告、日志为准。"
    content = summary + "\n\n---\n\n" + "\n\n---\n\n".join(supplements)
    content += "\n\n生成时间：" + datetime.now(ZoneInfo("Asia/Shanghai")).isoformat() + "\n"
    email_content = "# 选股与热点补充报告\n\n"
    email_content += f"分析开始：{started_at:%Y-%m-%d %H:%M}（北京时间）\n\n"
    email_content += "；".join(f"{r['task']}：{r['status']}" for r in results) + "\n\n"
    email_content += "\n\n---\n\n".join(email_supplements)
    if repository := os.getenv("GITHUB_REPOSITORY"):
        if run_id := os.getenv("GITHUB_RUN_ID"):
            url = f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{run_id}"
            email_content += f"\n\n[完整报告与运行状态](<{url}>)（页面底部 Artifacts → analysis-reports-*，含逐股证据与诊断）。"
    email_content += f"\n\n生成：{datetime.now(ZoneInfo('Asia/Shanghai')):%Y-%m-%d %H:%M}（北京时间）\n"
    (REPORT_DIR / "actions_summary.md").write_text(content, encoding="utf-8")
    (REPORT_DIR / "actions_email.md").write_text(email_content, encoding="utf-8")
    (REPORT_DIR / "actions_status.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if path := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as output:
            output.write(content)
    notification_failed = False
    if notify and supplements and any(r["exit_code"] != SKIPPED for r in results):
        from src.notification import NotificationService

        notifier = NotificationService()
        notification_failed = not notifier.is_available() or not notifier.send(email_content)
        if notification_failed:
            print("::error::Supplement notification failed; reports remain available as artifacts")
    return 1 if notification_failed or any(r["exit_code"] not in (0, SKIPPED) for r in results) else 0


def snapshot_state(action: str) -> None:
    # Public-repository caches must be an explicit opt-in; never cache .env or credentials.
    database = Path("data/stock_analysis.db")
    snapshot = Path("data/actions-state/stock_analysis.db")
    source, destination = (database, snapshot) if action == "backup" else (snapshot, database)
    if not source.is_file():
        print(f"No {action} source; starting without saved history")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    if action == "restore" and destination.exists():
        raise ValueError("Refusing to overwrite an existing runtime database")
    with closing(sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("History database integrity check failed")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=("screening", "hotspots", "backtest", "etf-rotation"))
    parser.add_argument("--state", choices=("restore", "backup"))
    args = parser.parse_args()
    if args.state:
        snapshot_state(args.state)
        return 0
    if args.task:
        text, code = run_service(args.task)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"actions_{args.task}.md").write_text(text, encoding="utf-8")
        return code
    return orchestrate()


if __name__ == "__main__":
    raise SystemExit(main())
