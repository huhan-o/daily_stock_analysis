"""Offline contracts for the optional Actions batch modes."""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from scripts import actions_report as batch

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_run(monkeypatch, tmp_path):
    for key in list(batch.os.environ):
        if key.startswith("ACTIONS_") or key in {"GITHUB_STEP_SUMMARY", "AGENT_SKILLS"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("ACTIONS_NOTIFY", "false")
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("text", ["true", "false", "TRUE", " false "])
def test_strict_boolean(text, monkeypatch):
    monkeypatch.setenv("ACTIONS_FORCE_RUN", text)
    assert batch.flag("ACTIONS_FORCE_RUN") == (text.strip().lower() == "true")


@pytest.mark.parametrize("text", ["yes", "1", "", "$(echo true)"])
def test_invalid_boolean_is_rejected(text, monkeypatch):
    monkeypatch.setenv("ACTIONS_FORCE_RUN", text)
    with pytest.raises(ValueError):
        batch.flag("ACTIONS_FORCE_RUN")


@pytest.mark.parametrize(
    "text", ["$(touch x)", "--debug", "600519;echo hi", "a/b", "A" * 30, ",".join(["600519"] * 21)]
)
def test_unsafe_stock_override_rejected(text, monkeypatch):
    monkeypatch.setenv("ACTIONS_STOCKS", text)
    with pytest.raises(ValueError):
        batch.stock_override()


def test_custom_stocks_match_existing_cli_contract(monkeypatch):
    monkeypatch.setenv("ACTIONS_STOCKS", "603993，600703 AAPL 000300.CSI")
    command = batch.main_command("stocks-only", batch.stock_override(), False, True)
    assert command == [sys.executable, "main.py", "--no-market-review", "--stocks",
                       "603993,600703,AAPL,000300.CSI", "--no-notify", "--force-run"]


@pytest.mark.parametrize("number", ["0", "11", "-1", "not-a-number"])
def test_candidate_bounds(number, monkeypatch):
    monkeypatch.setenv("ACTIONS_MAX_RESULTS", number)
    with pytest.raises(ValueError):
        batch.orchestrate()


def test_full_keeps_original_report_and_continues_after_failure(monkeypatch):
    calls = []
    def run(command, timeout, env):
        calls.append(command)
        return 1 if len(calls) == 1 else 0
    monkeypatch.setattr(batch, "run_command", run)
    assert batch.orchestrate() == 1
    assert calls[0] == [sys.executable, "main.py", "--no-notify"]
    assert [c[-1] for c in calls[1:]] == ["hotspots", "screening"]
    results = json.loads(Path("reports/actions_status.json").read_text())
    assert results[0]["status"] == "失败"
    assert len(results) == 3
    assert "未产生可用报告" in Path("reports/actions_summary.md").read_text()


def test_full_extras_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ACTIONS_DAILY_EXTRAS", "false")
    calls = []
    monkeypatch.setattr(batch, "run_command", lambda *args: calls.append(args) or 0)
    assert batch.orchestrate() == 0
    assert len(calls) == 1


def test_data_check_never_sends_and_never_runs_llm(monkeypatch):
    monkeypatch.setenv("ACTIONS_MODE", "data-check")
    monkeypatch.setenv("ACTIONS_NOTIFY", "true")
    calls = []
    monkeypatch.setattr(batch, "run_command", lambda command, *args: calls.append(command) or 0)
    assert batch.orchestrate() == 0
    assert "--dry-run" in calls[0] and "--no-notify" in calls[0]
    assert calls[1][-1] == "--check-notify"


def test_strategy_only_sets_agent_in_child(monkeypatch):
    monkeypatch.setenv("ACTIONS_MODE", "strategy")
    calls = []
    monkeypatch.setattr(batch, "run_command", lambda command, timeout, env: calls.append((command, env)) or 0)
    assert batch.orchestrate() == 0
    assert calls[0][1]["AGENT_MODE"] == "true"
    assert "--no-market-review" in calls[0][0]
    assert batch.os.getenv("AGENT_MODE") != "true"


def test_unknown_skill_and_unknown_mode_fail_before_network(monkeypatch):
    monkeypatch.setenv("ACTIONS_MODE", "strategy")
    monkeypatch.setenv("ACTIONS_AGENT_SKILLS", "typo_skill")
    with pytest.raises(ValueError, match="Unknown Agent skill"):
        batch.orchestrate()
    monkeypatch.setenv("ACTIONS_MODE", "shell")
    with pytest.raises(ValueError, match="Unsupported Actions mode"):
        batch.orchestrate()


def test_empty_etf_environment_does_not_override_application_defaults(monkeypatch):
    monkeypatch.setenv("ACTIONS_MODE", "etf-rotation")
    monkeypatch.setenv("ETF_ROTATION_POOL", "")
    monkeypatch.setenv("ETF_ROTATION_SAFE_ASSET", "")
    def run(command, timeout, env):
        assert "ETF_ROTATION_POOL" not in env
        assert "ETF_ROTATION_SAFE_ASSET" not in env
        return 0
    monkeypatch.setattr(batch, "run_command", run)
    assert batch.orchestrate() == 0


def test_screening_preserves_zero_scores_and_escapes_table():
    content, code = batch.render_screening({"ranking_mode": "llm", "candidates": [
        {"code": "600703", "name": "A|B<script>", "score": 0, "reason": "test\nreason"}]})
    assert code == 0
    assert "| 0 |" in content and "A\\|B&lt;script&gt;" in content
    assert "test reason" in content


@pytest.mark.parametrize("extra", [{"llm_failure_reason": "timeout"}, {"warnings": ["stale snapshot"]},
                                  {"source_errors": ["data unavailable"]}, {"ranking_mode": "factor"}])
def test_screening_degraded_status(extra):
    data = {"ranking_mode": "llm", "candidates": [{"code": "600703"}], **extra}
    content, code = batch.render_screening(data)
    assert code == batch.PARTIAL and "降级" in content


def test_empty_results_and_stale_hotspots_are_not_marked_successful():
    assert batch.render_screening({"ranking_mode": "llm"})[1] == batch.PARTIAL
    assert batch.render_hotspots({})[1] == batch.PARTIAL
    content, code = batch.render_hotspots({"cache_used": True, "cached_at": "2026-10-10",
                                         "hotspots": [{"topic": "半导体", "change_pct": 0}]})
    assert code == batch.PARTIAL and "2026-10-10" in content and "| 0 |" in content


def test_backtest_without_samples_never_invents_accuracy():
    content, code = batch.render_backtest({"processed": 0}, None)
    assert code == 0 and "尚无足够的成熟样本" in content
    assert "0%" not in content
    assert batch.render_backtest({"errors": 1}, None)[1] == batch.PARTIAL


def test_service_uses_existing_screening_contract(monkeypatch):
    calls = []
    config = SimpleNamespace(screening_enabled=False, trading_day_check_enabled=True)
    class Service:
        def __init__(self, cfg, db_manager):
            assert cfg.screening_enabled and db_manager == "db"
        def screen(self, **kwargs):
            calls.append(kwargs)
            return {"ranking_mode": "llm", "candidates": [{"code": "600703"}]}
        def hotspots(self, **kwargs):
            calls.append(kwargs)
            return {"hotspots": [{"topic": "theme"}]}
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(get_config=lambda: config))
    monkeypatch.setitem(
        sys.modules, "src.storage", SimpleNamespace(DatabaseManager=SimpleNamespace(get_instance=lambda: "db"))
    )
    monkeypatch.setitem(sys.modules, "src.services.screening_service", SimpleNamespace(ScreeningService=Service))
    monkeypatch.setattr(batch, "attach_technical_plans", lambda data: data['candidates'][0].update(
        technical_plan={"status": "无法测算", "reason": "test data unavailable"}))
    monkeypatch.setitem(
        sys.modules, "src.core.trading_calendar", SimpleNamespace(get_open_markets_today=lambda: {"cn"})
    )
    assert batch.run_service("screening")[1] == batch.PARTIAL
    assert calls[0] == {"strategy": "balanced_alpha", "market": "cn", "max_results": 10, "research_limit": 12}
    assert batch.run_service("hotspots")[1] == 0
    assert calls[1] == {"top": 12, "refresh": True, "include_details": False}
    monkeypatch.setitem(sys.modules, "src.core.trading_calendar", SimpleNamespace(get_open_markets_today=lambda: set()))
    assert batch.run_service("screening")[1] == batch.SKIPPED
    monkeypatch.setenv("ACTIONS_FORCE_RUN", "true")
    assert batch.run_service("screening")[1] == batch.PARTIAL


@pytest.mark.parametrize("notify_success", [True, False])
def test_supplement_notification_and_summary(monkeypatch, tmp_path, notify_success):
    monkeypatch.setenv("ACTIONS_NOTIFY", "true")
    monkeypatch.setenv("ACTIONS_MODE", "hotspots")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    sent = []
    class Notifier:
        def is_available(self):
            return True
        def send(self, text):
            sent.append(text)
            return notify_success
    monkeypatch.setitem(sys.modules, "src.notification", SimpleNamespace(NotificationService=Notifier))
    def run(*args):
        Path("reports/actions_hotspots.md").write_text("# 真实任务输出")
        return 0
    monkeypatch.setattr(batch, "run_command", run)
    assert batch.orchestrate() == (0 if notify_success else 1)
    assert len(sent) == 1 and "真实任务输出" in sent[0]
    assert "hotspots" in (tmp_path / "summary.md").read_text()


def test_skipped_only_does_not_send_supplement(monkeypatch):
    monkeypatch.setenv("ACTIONS_NOTIFY", "true")
    monkeypatch.setenv("ACTIONS_MODE", "hotspots")
    monkeypatch.setattr(batch, "run_command", lambda *args: batch.SKIPPED)
    assert batch.orchestrate() == 0


def test_timeout_kills_process_group(monkeypatch):
    killed, waits = [], []
    class Process:
        pid = 123
        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("command", timeout)
    monkeypatch.setattr(batch.subprocess, "Popen", lambda command, **kwargs: Process())
    monkeypatch.setattr(batch.os, "killpg", lambda pid, signal: killed.append(pid))
    assert batch.run_command(["safe"], 60, {}) == 124
    assert killed == [123] and waits == [60, None]


def test_sqlite_backup_includes_wal_and_restore_is_valid(tmp_path):
    Path("data").mkdir()
    source = sqlite3.connect("data/stock_analysis.db")
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE history(value TEXT)")
        source.execute("INSERT INTO history VALUES ('analysis')")
        source.commit()
        batch.snapshot_state("backup")
    finally:
        source.close()
    with pytest.raises(ValueError, match="overwrite"):
        batch.snapshot_state("restore")
    Path("data/stock_analysis.db").unlink()
    batch.snapshot_state("restore")
    with sqlite3.connect("data/stock_analysis.db") as restored:
        assert restored.execute("SELECT value FROM history").fetchall() == [("analysis",)]
    assert sorted(p.name for p in Path("data/actions-state").iterdir()) == ["stock_analysis.db"]


def test_missing_and_invalid_state(tmp_path):
    batch.snapshot_state("restore")
    Path("data/actions-state").mkdir(parents=True)
    Path("data/actions-state/stock_analysis.db").write_text("invalid database")
    with pytest.raises(sqlite3.DatabaseError):
        batch.snapshot_state("restore")
    assert not Path("data/stock_analysis.db").exists()


def test_workflow_exposes_modes_safely_and_history_is_opt_in():
    text = (ROOT / ".github/workflows/00-daily-analysis.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow["on"]["schedule"] == [{"cron": "0 10 * * 1-5"}]
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs["mode"]["options"]) == set(batch.MODES)
    assert set(inputs["screening_strategy"]["options"]) == set(batch.STRATEGIES)
    assert len(inputs) <= 10
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["analyze"]["steps"]
    execution = next(s for s in steps if s.get("id") == "analysis")
    assert "python -m scripts.actions_report" in execution["run"]
    assert "${{ github.event.inputs." not in execution["run"]
    assert execution["env"]["BOCHA_API_KEYS"] == "${{ secrets.BOCHA_API_KEYS }}"
    assert execution["env"]["DATABASE_PATH"] == "./data/stock_analysis.db"
    for step in steps:
        if step.get("uses", "").startswith("actions/cache/"):
            assert step["with"]["path"] == "data/actions-state/stock_analysis.db"
            assert step.get("if")
    restore = next(s for s in steps if s.get("id") == "history-cache")
    assert restore["if"] == "vars.ACTIONS_PERSIST_HISTORY == 'true'"
    assert {p.stem for p in (ROOT / "src/services/screening/strategies").glob("*.yaml")} == set(batch.STRATEGIES)


def test_regular_reports_cannot_auto_enable_agent_from_configured_skills(monkeypatch):
    monkeypatch.setenv("ACTIONS_AGENT_SKILLS", "all")
    monkeypatch.setenv("AGENT_SKILLS", "bull_trend")
    monkeypatch.setenv("AGENT_MODE", "true")
    def run(command, timeout, env):
        assert env["AGENT_MODE"] == "false"
        assert env["AGENT_SKILLS"] == ""
        return 0
    monkeypatch.setattr(batch, "run_command", run)
    assert batch.orchestrate() == 0


def test_manual_strategy_forwards_only_valid_skills(monkeypatch):
    Path("strategies").mkdir()
    Path("strategies/bull_trend.yaml").touch()
    monkeypatch.setenv("ACTIONS_MODE", "strategy")
    monkeypatch.setenv("ACTIONS_AGENT_SKILLS", "bull_trend")
    def run(command, timeout, env):
        assert env["AGENT_SKILLS"] == "bull_trend"
        assert env["AGENT_MODE"] == "true"
        return 0
    monkeypatch.setattr(batch, "run_command", run)
    assert batch.orchestrate() == 0


@pytest.mark.parametrize("partial", [False, True])
def test_etf_service_saves_output_and_propagates_data_warnings(monkeypatch, partial):
    config = object()
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(get_config=lambda: config))
    def run(cfg, *, send_notification):
        assert cfg is config and send_notification is False
        return SimpleNamespace(
            markdown="# ETF report", failed_codes={},
            warnings=["short history"] if partial else [], as_of="2026-10-15",
        )
    monkeypatch.setitem(sys.modules, "src.services.etf_rotation_service", SimpleNamespace(run_etf_rotation=run))
    assert batch.run_service("etf-rotation") == ("# ETF report", batch.PARTIAL if partial else 0)


def test_backtest_service_reuses_summary_api(monkeypatch):
    calls = []
    class Service:
        def run_backtest(self):
            calls.append("run")
            return {"processed": 1, "completed": 1, "errors": 0}
        def get_global_summary(self):
            calls.append("summary")
            return {"completed_count": 1, "win_rate_pct": 0}
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(get_config=lambda: object()))
    monkeypatch.setitem(sys.modules, "src.services.backtest_service", SimpleNamespace(BacktestService=Service))
    text, code = batch.run_service("backtest")
    assert calls == ["run", "summary"]
    assert code == 0 and "胜率（%）：0" in text


def test_actual_subprocess_exit_code_is_preserved():
    assert batch.run_command([sys.executable, "-c", "raise SystemExit(7)"], 5, batch.os.environ.copy()) == 7


def screening_mail_data():
    today = str(batch.datetime.now(batch.ZoneInfo("Asia/Shanghai")).date())
    return {"ranking_mode": "llm", "llm_market_view": "普涨但银行偏弱", "llm_portfolio_risk": "金融集中",
            "candidates": [{"code": str(600001 + i), "name": f"测试股{i}", "reason": "低估值待确认",
                            "llm_thesis": "估值与量能均衡",
                            "risk_summary": "信用风险", "score": 75,
                            "why_selected": [{"source": "llm", "quality": "inferred", "text": "估值与量能均衡"},
                                             {"source": "screening", "text": "internal factor labels"}],
                            "technical_plan": {"status": "条件式技术测算", "source": "test", "as_of": today,
                                               "entry": 10, "target": 13, "take_profit": 12, "stop_loss": 9,
                                               "ma5": 10, "ma20": 10, "support20": 9,
                                               "resistance20": 11, "atr20": 1},
                            "dsa_context": {"quote": {"price": 10, "change_pct": 0, "volume_ratio": 1.2,
                                                       "source": "test", "is_stale": True}},
                            "dsa_news": [{"title": f"测试股{i}：季报发布", "published_date": today,
                                          "url": f"https://example.com/{i}", "snippet": "网页登录噪声"}]}
                           for i in range(10)]}


def test_compact_screening_preserves_all_stocks_prices_and_uncertainty():
    data = screening_mail_data()
    compact, code = batch.render_screening(data, requested=10, compact=True)
    detailed, detailed_code = batch.render_screening(data, requested=10)
    assert code == detailed_code == 0
    assert compact.count("## ") == 10
    assert compact.count("参考入场 10.00｜目标 13.00｜止盈 12.00｜止损 9.00") == 10
    quotes = [line for line in compact.splitlines() if line.startswith("- 行情：")]
    assert len(quotes) == 10 and all("行情时间未核验" in line and "行情已过期" in line for line in quotes)
    assert "普涨但银行偏弱" in compact and "金融集中" in compact
    assert compact.count("- 理由：估值与量能均衡") == 10 and "低估值待确认" not in compact
    assert "inferred" not in compact and "internal factor labels" not in compact
    assert "网页登录噪声" not in compact and "网页登录噪声" in detailed
    assert compact.count("价格口径") == 1 and len(compact) < len(detailed) / 2
    for i in range(10):
        assert f"测试股{i}（{600001 + i}）" in compact
        assert f"https://example.com/{i}" in compact


def test_compact_news_filters_quote_pages_mismatches_dates_and_unsafe_urls():
    from datetime import timedelta
    data = screening_mail_data()
    stock = data["candidates"][0]
    today = batch.datetime.now(batch.ZoneInfo("Asia/Shanghai")).date()
    stock["dsa_news"] = [
        {"title": "测试股0:10.00 0.00% 行情", "url": "https://q.stock.sohu.com/cn/600001", "published_date": str(today)},
        {"title": "其他公司减持", "url": "https://example.com/unrelated", "published_date": str(today)},
        {"title": "测试股0旧资料", "url": "https://example.com/old", "published_date": str(today - timedelta(days=8))},
        {"title": "测试股0未核验", "url": "https://example.com/undated"},
        {"title": "测试股0未来资料", "url": "https://example.com/future", "published_date": str(today + timedelta(days=1))},
        {"title": "测试股0危险链接", "url": "javascript:alert(1)", "published_date": str(today)},
        *[{"title": f"测试股0公告{n}", "url": f"https://example.com/notice{n}", "published_date": str(today)} for n in range(3)],
    ]
    content, _ = batch.render_screening(data, compact=True)
    assert "notice0" in content and "notice1" in content and "notice2" not in content
    for forbidden in ("sohu", "unrelated", "/old", "undated", "future", "javascript:"):
        assert forbidden not in content
    stock["dsa_news"] = stock["dsa_news"][:6]
    content, _ = batch.render_screening(data, compact=True)
    assert "暂无近7日匹配本股的有效新闻链接" in content


@pytest.mark.parametrize("failure", ["shortfall", "price", "model", "source", "enrichment"])
def test_compact_and_detailed_reports_share_partial_status(failure):
    data = screening_mail_data()
    if failure == "shortfall":
        data["candidates"].pop()
    elif failure == "price":
        data["candidates"][0]["technical_plan"] = {"status": "无法测算", "reason": "日线过期"}
    elif failure == "model":
        data["llm_failure_reason"] = "timeout"
    elif failure == "source":
        data["source_errors"] = ["source failed"]
    else:
        data["dsa_enrichment"] = {"warnings": ["news missing"]}
    compact, code = batch.render_screening(data, requested=10, compact=True)
    assert code == batch.render_screening(data, requested=10)[1] == batch.PARTIAL
    assert "部分结果" in compact
    if failure == "price":
        assert "无法测算（日线过期）" in compact
    if failure == "shortfall":
        assert "候选 9 只 / 请求 10 只" in compact


def test_notification_uses_compact_content_while_artifacts_retain_details(monkeypatch):
    monkeypatch.setenv("ACTIONS_MODE", "screening")
    monkeypatch.setenv("ACTIONS_NOTIFY", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    sent = []
    class Notifier:
        def is_available(self):
            return True
        def send(self, text):
            sent.append(text)
            return True
    monkeypatch.setitem(sys.modules, "src.notification", SimpleNamespace(NotificationService=Notifier))
    def run(*args):
        data = screening_mail_data()
        Path("reports/actions_screening.md").write_text(batch.render_screening(data, requested=10)[0])
        Path("reports/actions_screening_email.md").write_text(batch.render_screening(data, requested=10, compact=True)[0])
        return 0
    monkeypatch.setattr(batch, "run_command", run)
    assert batch.orchestrate() == 0
    assert sent == [Path("reports/actions_email.md").read_text()]
    assert "网页登录噪声" not in sent[0] and "网页登录噪声" in Path("reports/actions_summary.md").read_text()
    assert "/example/repo/actions/runs/123" in sent[0]


def test_failed_rerun_cannot_send_leftover_compact_report(monkeypatch):
    monkeypatch.setenv("ACTIONS_MODE", "screening")
    Path("reports").mkdir()
    Path("reports/actions_screening_email.md").write_text("过期的成功报告")
    monkeypatch.setattr(batch, "run_command", lambda *args: 124)
    assert batch.orchestrate() == 1
    assert "过期的成功报告" not in Path("reports/actions_email.md").read_text()
    assert "失败" in Path("reports/actions_email.md").read_text()
