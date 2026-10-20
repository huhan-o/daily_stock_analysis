"""Risk groups must change the candidate universe before model ranking."""

from copy import deepcopy
from datetime import timedelta

import pandas as pd
import pytest

from scripts.actions_report import render_recommendations
from scripts.actions_risk_screening import build_recommendations, hotspot_members
from src.services.screening.recommendations import diverse_rows, filter_risk, filter_universe, risk_band


def daily(**changes):
    return {"atr_20_pct": 1.5, "volatility_20d_pct": 20, "max_drawdown_20d_pct": -4,
            "change_pct": 2, "turnover_rate": 3, "daily_quality_score": 100,
            "daily_quality_flags": "", **changes}


@pytest.mark.parametrize("changes,expected", [
    ({}, "low"), ({"atr_20_pct": 3}, "medium"), ({"atr_20_pct": 5}, "high"),
    ({"volatility_20d_pct": 50}, "high"), ({"max_drawdown_20d_pct": -15}, "high"),
    ({"turnover_rate": 12}, "high"), ({"atr_20_pct": None}, ""),
    ({"volatility_20d_pct": float("nan")}, ""), ({"atr_20_pct": float("inf")}, ""),
    ({"daily_quality_flags": "stale_cache"}, ""), ({"daily_quality_score": 0}, ""),
    ({"daily_quality_flags": "invalid_ohlc"}, ""), ({"change_pct": 9.8}, ""),
    ({"max_drawdown_20d_pct": -30}, ""),
])
def test_risk_requires_observed_valid_daily_metrics(changes, expected):
    assert risk_band(daily(**changes)) == expected


def test_hotspot_membership_and_exclusions_apply_before_scores():
    frame = pd.DataFrame([{"code": "600001", "concepts": "原题材"}, {"code": "600002"}, {"code": "600003"}])
    policy = {"group": "high", "excluded_codes": ["600003"],
              "members": {"600002": {"topic": "机器人", "board_heat_score": 90}, "600003": {"topic": "芯片"}}}
    filtered = filter_universe(frame, policy)
    assert filtered.code.tolist() == ["600002"]
    assert filtered.iloc[0].concepts == "机器人"
    assert filtered.iloc[0].board_heat_score == 90
    assert frame.code.tolist() == ["600001", "600002", "600003"]
    assert filter_universe(frame, {"group": "high", "members": {}}).empty


def test_medium_hotspots_allow_calm_members_but_reject_high_risk():
    frame = pd.DataFrame([daily(code="600001"), daily(code="600002", atr_20_pct=3),
                          daily(code="600003", atr_20_pct=5), daily(code="600004", atr_20_pct=None)])
    assert filter_risk(frame, "medium").code.tolist() == ["600001", "600002"]
    assert filter_risk(frame, "high").code.tolist() == ["600003"]
    assert filter_risk(frame, "low").code.tolist() == ["600001"]


def test_concentration_is_a_hard_cap_even_when_bank_scores_are_highest():
    rows = [{"code": f"{600000+i}", "name": f"银行{i}", "score": 100-i} for i in range(15)]
    rows += [{"code": f"{600100+i}", "industry": f"行业{i//3}", "recommendation_topic": f"主题{i//3}"}
             for i in range(15)]
    selected = diverse_rows(rows, 10)
    assert len(selected) == 10
    assert sum("银行" in row.get("name", "") for row in selected) == 2
    assert not any("银行" in row.get("name", "") for row in diverse_rows(rows, 10, bank_budget=0))


class Service:
    def __init__(self, *, stale=False, fail_hotspots=False):
        self.calls = []
        self.stale = stale
        self.fail_hotspots = fail_hotspots

    def hotspots(self, **kwargs):
        if self.fail_hotspots:
            raise RuntimeError("offline")
        return {"stale": self.stale, "hotspots": [
            {"topic": f"主题{i}", "change_pct": 3, "heat_score": 90} for i in range(12)]}

    def hotspot_detail(self, topic, **kwargs):
        i = int(topic[2:])
        return {"provider": "fixture", "stocks": [{"code": str(600000 + i*10+j)} for j in range(10)]}

    def screen(self, **kwargs):
        self.calls.append(kwargs)
        policy = kwargs["recommendation_policy"]
        group = policy["group"]
        start = {"high": 0, "medium": 40, "low": 80}[group]
        atr = {"high": 5, "medium": 3, "low": 1.5}[group]
        return {"ranking_mode": "llm", "candidates": [
            {"code": str(600000+start+i), "name": f"股票{start+i}", "industry": f"行业{i//3}",
             "raw": daily(atr_20_pct=atr), "llm_thesis": "数据支持的研究判断"} for i in range(12)]}


def plans(data):
    for item in data["candidates"]:
        item["technical_plan"] = {"status": "条件式技术测算", "entry": 10, "stop_loss": 9,
                                  "take_profit": 12, "target": 13, "as_of": "2026-10-19", "source": "fixture",
                                  "ma5": 10, "ma20": 10, "support20": 9, "resistance20": 11, "atr20": .5}


def test_three_groups_have_thirty_unique_stocks_complete_prices_and_reasons():
    service = Service()
    # Four distinct verified themes per 12-stock shortlist.
    def detail(topic, **kwargs):
        theme = int(topic[2:])
        return {"provider": "fixture", "stocks": [
            {"code": str(600000+start+i)} for start in (0, 40) for i in range(theme*3, theme*3+3)]}
    service.hotspot_detail = detail
    checkpoints = []
    result = build_recommendations(service, 10, plans, on_group=lambda data: checkpoints.append(deepcopy(data)))
    assert [len(group["candidates"]) for group in result["groups"]] == [10, 10, 10]
    assert len({item["code"] for item in result["candidates"]}) == 30
    assert len(checkpoints) == 4 and len(checkpoints[1]["groups"]) == 1
    assert [call["strategy"] for call in service.calls] == ["capital_heat", "momentum_quality", "low_volatility_quality"]
    assert len(service.calls[-1]["recommendation_policy"]["excluded_codes"]) == 20
    content, status = render_recommendations(result, compact=True)
    assert status == 0
    assert "目标 30 只，实际 30 只" in content
    assert "高风险（10/10只）" in content and "中风险（10/10只）" in content and "低风险股票（10/10只）" in content
    assert "目标购入价 | 止损价 | 止盈价" in content
    assert content.count("| 10.00 | 9.00 | 12.00 |") == 30
    assert all(item["recommendation_reason"] for item in result["candidates"])


@pytest.mark.parametrize("stale,failed", [(True, False), (False, True)])
def test_hotspot_failure_does_not_fill_with_non_hot_stocks_or_block_low_group(stale, failed):
    service = Service(stale=stale, fail_hotspots=failed)
    result = build_recommendations(service, 10, plans)
    assert [len(group["candidates"]) for group in result["groups"]] == [0, 0, 10]
    assert len(service.calls) == 1
    assert render_recommendations(result, compact=True)[1] == 2


def test_prices_missing_or_inverted_are_excluded_from_recommendations():
    service = Service(fail_hotspots=True)
    def invalid(data):
        plans(data)
        for item in data["candidates"]:
            item["technical_plan"]["stop_loss"] = 11
    result = build_recommendations(service, 10, invalid)
    assert not result["candidates"]
    assert "数量不足" in render_recommendations(result, compact=True)[0]


def test_stale_or_related_topic_stock_is_not_verified_membership():
    service = Service()
    service.hotspot_detail = lambda **kwargs: {"stocks": [
        {"code": "600001", "source": "related_hotspot"}, {"code": "600002", "fallback_used": True},
        {"code": "600003", "stale": True}, {"code": "600004", "source": "constituents"}]}
    members, _ = hotspot_members(service)
    assert set(members) == {"600004"}


@pytest.mark.parametrize("group,code", [("high", "600001"), ("medium", "600002"), ("low", "600003")])
def test_real_pipeline_filters_daily_risk_before_shortlisting(monkeypatch, tmp_path, group, code):
    from src.core.trading_calendar import get_effective_trading_date, get_trading_dates
    from src.services.screening import pipeline
    from src.services.screening.config import Config

    (tmp_path / "fixture.yaml").write_text(
        "name: fixture\ndisplay_name: fixture\ndescription: fixture\nscreening:\n  enabled: true\n"
        "  market_scope: [cn]\n  hard_filters:\n    exclude_st: true\n", encoding="utf-8")
    snapshot = pd.DataFrame([
        {"code": value, "name": value, "price": 10, "change_pct": 2, "amount": 5e8,
         "total_mv": 20e9, "pe_ratio": 20, "pb_ratio": 2, "turnover_rate": 3, "volume_ratio": 2}
        for value in ("600001", "600002", "600003", "600004")])
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", lambda *a, **kw: snapshot)
    expected = get_effective_trading_date("cn")
    sessions = get_trading_dates("cn", expected - timedelta(days=180), expected)[-90:]
    def history(stock_code, **kwargs):
        spread = {"600001": .3, "600002": .15, "600003": .05, "600004": .3}[stock_code]
        return pd.DataFrame({"date": sessions, "open": 10,
                             "high": 10+spread, "low": 10-spread, "close": 10, "volume": 10000})
    policy = {"group": group, "members": {value: {"topic": "机器人"} for value in ("600001", "600002", "600003")}}
    config = Config(strategies_dir=tmp_path, daily_enrich_enabled=True, daily_fetch_max_workers=1,
                    llm_max_candidates=12, post_analyzers=[], risk_enabled=False, portfolio_diversity_enabled=False)
    result = pipeline.screen("fixture", config=config, use_llm=False,
                             context={"recommendations": policy}, daily_history_fetcher=history)
    # The non-hot stock 600004 has higher-risk bars too; it must never enter a hotspot group.
    assert [pick.code for pick in result.picks] == ([code] if group != "medium" else ["600002", "600003"]), result.degradation
    if group != "low":
        assert all("机器人" in pick.concepts for pick in result.picks)


def test_service_passes_risk_policy_to_real_pipeline_bridge(monkeypatch):
    from src.config import Config
    from src.services import screening_service
    from src.services.screening.models import ScreenResult

    received = []
    def core(strategy, **kwargs):
        received.append(kwargs)
        return ScreenResult(strategy=strategy, market="cn")
    monkeypatch.setattr(screening_service, "run_screening_pipeline", core)
    policy = {"group": "high", "members": {"600001": {"topic": "机器人"}}}
    service = screening_service.ScreeningService(Config(screening_enabled=True))
    service.screen(strategy="capital_heat", market="cn", max_results=12, research_limit=12,
                   recommendation_policy=policy)
    assert received[0]["context"]["recommendations"] == policy
    assert received[0]["config"].daily_enrich_enabled is True
    assert received[0]["config"].daily_enrich_max_candidates == 48


def test_default_actions_entry_builds_group_reports_and_checkpoints(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from scripts import actions_report as batch

    service = Service(fail_hotspots=True)
    config = SimpleNamespace(screening_enabled=False, trading_day_check_enabled=False)
    monkeypatch.delenv("ACTIONS_SCREENING_STRATEGY", raising=False)
    monkeypatch.delenv("ACTIONS_MAX_RESULTS", raising=False)
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(get_config=lambda: config))
    monkeypatch.setitem(sys.modules, "src.storage", SimpleNamespace(DatabaseManager=SimpleNamespace(get_instance=lambda: None)))
    monkeypatch.setitem(sys.modules, "src.services.screening_service", SimpleNamespace(ScreeningService=lambda *a, **kw: service))
    monkeypatch.setattr(batch, "attach_technical_plans", plans)
    monkeypatch.setattr(batch, "REPORT_DIR", tmp_path)
    content, status = batch.run_service("screening")
    assert status == batch.PARTIAL and "低风险股票（10/10只）" in content
    assert "目标购入价" in (tmp_path / "actions_screening_email.md").read_text()
    assert "高风险（0/10只）" in (tmp_path / "actions_screening.md").read_text()
