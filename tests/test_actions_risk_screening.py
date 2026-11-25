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


def test_hotspot_membership_uses_full_board_not_top_movers():
    # 热度前 30 名几乎都是当日大涨股，会被高/中风险组的涨幅上限全部挡掉，必须取全部成分股。
    service = Service()
    requested = []
    def detail(topic, **kwargs):
        requested.append(kwargs.get("top_stocks"))
        return {"provider": "fixture", "stocks": [{"code": str(600000 + j)} for j in range(120)]}
    service.hotspot_detail = detail
    members, _ = hotspot_members(service)
    assert requested and all((value or 0) > 30 for value in requested)
    assert len(members) == 120


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
    assert received[0]["config"].daily_enrich_max_candidates == 240


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


def test_research_pool_balances_themes_without_spending_final_quotas():
    from src.services.screening.recommendations import diverse_frame

    rows = [{"code": str(600000 + i), "name": f"银行{i}", "industry": "银行",
             "recommendation_topic": "金融"} for i in range(20)]
    rows += [{"code": "600100", "industry": "机械", "recommendation_topic": "机器人"},
             {"code": "600101", "industry": "电子", "recommendation_topic": "芯片"}]
    frame = pd.DataFrame(rows)
    policy = {"bank_budget": 0, "dividend_budget": 0}
    research = diverse_frame(frame, 12, policy, research=True)
    assert len(research) == 12
    assert {"600100", "600101"}.issubset(set(research.code))
    assert research.code.tolist()[:3] == ["600000", "600100", "600101"]
    assert sum(research.industry == "银行") == 10
    # Final portfolio constraints remain strict.
    assert len(diverse_frame(frame, 12, policy)) == 2


def test_wider_hotspot_list_is_positive_fresh_unique_and_bounded():
    service = Service()
    requested, topics = [], []
    def hotspots(**kwargs):
        requested.append(kwargs)
        return {"hotspots": [
            {"topic": f"下跌{i}", "change_pct": -1} for i in range(12)
        ] + [{"topic": "过期", "change_pct": 2, "stale": True}] + [
            {"topic": f"主题{i//2}", "change_pct": 2} for i in range(50)
        ]}
    def detail(topic, **kwargs):
        topics.append(topic)
        return {"canonical_topic": topic, "stocks": [{"code": str(600000 + int(topic[2:]))}]}
    service.hotspots, service.hotspot_detail = hotspots, detail
    coverage = {}
    members, errors = hotspot_members(service, coverage)
    assert requested[0]["top"] == 50
    assert len(topics) == len(set(topics)) == len(members) == 20
    assert coverage == {"hotspot_boards": 20, "hotspot_members": 20}
    assert not errors


def test_final_plan_validation_can_backfill_beyond_original_twelve():
    service = Service(fail_hotspots=True)
    def screen(**kwargs):
        service.calls.append(kwargs)
        return {"ranking_mode": "llm", "candidates": [
            {"code": str(600000+i), "name": f"股票{i}", "industry": f"行业{i//3}",
             "raw": daily(), "llm_thesis": "规则研究"} for i in range(24)]}
    service.screen = screen
    def validate(data):
        plans(data)
        for item in data["candidates"][:12]:
            item["technical_plan"]["stop_loss"] = 11
    result = build_recommendations(service, 10, validate)
    low = result["groups"][-1]
    assert service.calls[0]["max_results"] == 30
    assert service.calls[0]["research_limit"] == 12
    assert service.calls[0]["recommendation_policy"]["defer_final_selection"] is True
    assert len(low["candidates"]) == 10
    assert all(int(item["code"]) >= 600012 for item in low["candidates"])
    assert low["selection_diagnostics"]["invalid_plan_or_risk_data"] == 12
    email, _ = render_recommendations(result, compact=True)
    assert "引擎候选 24 → 价格/理由有效 12 → 最终 10" in email
    assert "本组暂无满足全部条件的候选" in email
    assert "模型分析未完成" not in email


def test_real_pipeline_keeps_thirty_reserves_despite_twelve_model_cap(monkeypatch, tmp_path):
    from src.services.screening import pipeline
    from src.services.screening.config import Config

    (tmp_path / "fixture.yaml").write_text(
        "name: fixture\ndisplay_name: fixture\ndescription: fixture\nscreening:\n  enabled: true\n"
        "  market_scope: [cn]\n  hard_filters:\n    exclude_st: true\n", encoding="utf-8")
    snapshot = pd.DataFrame([
        {"code": str(600000+i), "name": f"股票{i}", "price": 10, "change_pct": 2, "amount": 5e8,
         "industry": "机械", "total_mv": 20e9, "pe_ratio": 20, "pb_ratio": 2,
         "turnover_rate": 3, "volume_ratio": 2} for i in range(80)])
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", lambda *a, **kw: snapshot)
    attempted = []
    def enrich(frame, **kwargs):
        attempted.append(len(frame))
        result = frame.copy()
        for key, value in daily(atr_20_pct=5).items():
            result[key] = value
        result.attrs["daily_success_count"] = len(result)
        return result
    monkeypatch.setattr(pipeline, "enrich_daily_features", enrich)
    policy = {"group": "high", "defer_final_selection": True,
              "members": {row["code"]: {"topic": "机器人"} for row in snapshot.to_dict("records")}}
    config = Config(strategies_dir=tmp_path, daily_enrich_enabled=True,
                    daily_enrich_max_candidates=240, llm_max_candidates=12,
                    post_analyzers=[], risk_enabled=False, portfolio_diversity_enabled=False)
    result = pipeline.screen("fixture", max_output=30, use_llm=False, config=config,
                             context={"recommendations": policy})
    assert attempted == [80]
    assert len(result.picks) == 30
    assert any("Recommendation risk band high: 80 -> 80" in note for note in result.degradation)
    # The final report, not the research engine, enforces max three per theme.
    rows = [{"code": pick.code, "recommendation_topic": "机器人"} for pick in result.picks]
    assert len(diverse_rows(rows, 10)) == 3


def test_recommendation_post_rank_only_reuses_research_without_new_searches(monkeypatch):
    from src.services import screening_service as service

    def forbidden(*args, **kwargs):
        raise AssertionError("post-rank must not spend a second news budget")
    monkeypatch.setattr(service, "_build_dsa_candidate_context", forbidden)
    candidates = [{"code": str(600000+i), "name": f"股票{i}"} for i in range(30)]
    cached = {"dsa_context": {"enriched": True, "news_included": True,
                              "events_included": True, "warnings": []},
              "dsa_news": [{"title": "已研究新闻"}]}
    enriched, metadata = service._enrich_candidates_with_dsa(
        candidates, max_candidates=12, cache_only=True,
        candidate_context_cache={"600029": cached})
    assert len(enriched) == 30
    assert enriched[-1]["dsa_news"] == cached["dsa_news"]
    assert metadata["cache_only"] is True
    assert metadata["enriched_count"] == metadata["requested_count"] == 1
    assert metadata["warnings"] == []


def test_live_membership_paginates_exact_board_without_leader_fallback(monkeypatch):
    from src.services.screening_service import DsaEastMoneyHotspotProvider
    from contextlib import nullcontext
    from types import SimpleNamespace

    provider = DsaEastMoneyHotspotProvider()
    monkeypatch.setattr(provider, "_source_call_budget", nullcontext)
    calls = []
    def get(url, **kwargs):
        params = kwargs["params"]
        scope, page = params["fs"], int(params["pn"])
        calls.append((scope, page))
        if scope.startswith("m:90"):
            data = {"total": 1, "diff": [{"f12": "BK0001", "f14": "机器人"}]}
        else:
            assert scope == "b:BK0001 f:!50"
            start, end = (0, 100) if page == 1 else (100, 135)
            data = {"total": 135, "diff": [{"f12": str(600000+i), "f14": f"股票{i}"}
                                              for i in range(start, end)]}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": data})
    monkeypatch.setattr(provider, "_eastmoney_get", get)
    first = provider.recommendation_constituents("机器人")
    second = provider.recommendation_constituents("机器人")
    assert len(first["stocks"]) == len(second["stocks"]) == 135
    assert first["canonical_topic"] == "机器人"
    assert first["fallback_used"] is False
    assert all(item["source"] == "eastmoney_live_constituents:BK0001" for item in first["stocks"])
    assert sum(scope.startswith("m:90") for scope, _ in calls) == 1
    with pytest.raises(ValueError, match="No exact"):
        provider.recommendation_constituents("不存在的板块")


def test_tushare_gate_is_shared_by_managers_and_smooth_not_bursty(monkeypatch):
    from data_provider import tushare_fetcher as module

    clock = [0.0]
    waits = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(module, "_TUSHARE_NEXT_CALL_AT", 0.0)
    first, second = module.TushareFetcher.__new__(module.TushareFetcher), module.TushareFetcher.__new__(module.TushareFetcher)
    for fetcher in (first, second):
        fetcher.rate_limit_per_minute = 40
        fetcher._call_count = 0
        fetcher._minute_start = None
    first._check_rate_limit()
    second._check_rate_limit()
    first._check_rate_limit()
    assert waits == [1.5, 1.5]
    assert first._call_count == 2 and second._call_count == 1


@pytest.mark.parametrize("new_api,method", [
    ("stock_zh_a_hist_em", "_fetch_stock_data_em"),
    ("stock_zh_a_hist_sina", "_fetch_stock_data_sina"),
])
def test_akshare_renamed_history_api_does_not_evaluate_missing_legacy(monkeypatch, new_api, method):
    import sys
    from types import SimpleNamespace
    from data_provider import akshare_fetcher as module

    calls = []
    def history(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame({"date": ["2026-11-20"], "open": [10], "high": [11],
                             "low": [9], "close": [10], "volume": [1000], "amount": [10000]})
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(**{new_api: history}))
    monkeypatch.setattr(module, "_akshare_call_with_timeout", lambda fn, **kw: fn(**{
        key: value for key, value in kw.items() if key not in {"timeout", "call_name"}}))
    fetcher = module.AkshareFetcher(sleep_min=0, sleep_max=0)
    monkeypatch.setattr(fetcher, "_set_random_user_agent", lambda: None)
    result = getattr(fetcher, method)("600001", "2026-11-01", "2026-11-20")
    assert len(result) == 1
    assert calls[0]["start_date"] == "20261101" and calls[0]["end_date"] == "20261120"
    assert calls[0]["adjust"] == "qfq"


@pytest.mark.parametrize("second", [[], [{"f12": "600001", "f14": "重复"}]])
def test_live_membership_rejects_incomplete_or_repeated_pages(monkeypatch, second):
    from types import SimpleNamespace
    from src.services.screening_service import DsaEastMoneyHotspotProvider

    provider = DsaEastMoneyHotspotProvider()
    def get(url, **kwargs):
        items = [{"f12": "600001", "f14": "第一只"}] if kwargs["params"]["pn"] == "1" else second
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"data": {"total": 2, "diff": items}})
    monkeypatch.setattr(provider, "_eastmoney_get", get)
    with pytest.raises(ValueError, match="Incomplete"):
        provider._recommendation_pages("b:BK0001 f:!50", fields="f12,f14", max_pages=5)


def test_recommendation_discovery_bypasses_heavy_hotspot_pipeline(monkeypatch):
    from src.config import Config
    from src.services import screening_service as module

    def forbidden(*args, **kwargs):
        raise AssertionError("heavy leader discovery must not run")
    monkeypatch.setattr(module.screening_hotspot, "discover_hotspots", forbidden)
    calls = []
    def pages(self, scope, **kwargs):
        calls.append(scope)
        return [{"f12": "BK0001" if "t:3" in scope else "BK0002",
                 "f14": "机器人" if "t:3" in scope else "机械", "f3": 2}]
    monkeypatch.setattr(module.DsaEastMoneyHotspotProvider, "_recommendation_pages", pages)
    service = module.ScreeningService(Config(screening_enabled=True))
    result = service.recommendation_hotspots(top=50)
    assert {item["topic"] for item in result["hotspots"]} == {"机器人", "机械"}
    assert len(calls) == 2
    assert service._recommendation_hotspot_provider._recommendation_board_codes["机器人"] == "BK0001"
    def detail(self, topic, **kwargs):
        assert self is service._recommendation_hotspot_provider
        return {"canonical_topic": topic, "stocks": [{"code": "600001"}]}
    monkeypatch.setattr(module.DsaEastMoneyHotspotProvider, "recommendation_constituents", detail)
    assert service.hotspot_detail(topic="机器人", top_stocks=500)["stocks"] == [{"code": "600001"}]


@pytest.mark.parametrize("scope,host", [
    ("m:90 t:3 f:!50", "79.push2.eastmoney.com"),
    ("m:90 t:2 f:!50", "17.push2.eastmoney.com"),
    ("b:BK0001 f:!50", "29.push2.eastmoney.com"),
])
def test_board_request_uses_documented_dedicated_host(monkeypatch, scope, host):
    from types import SimpleNamespace
    from src.services.screening_service import DsaEastMoneyHotspotProvider

    provider = DsaEastMoneyHotspotProvider()
    def get(url, **kwargs):
        assert url == f"https://{host}/api/qt/clist/get"
        assert kwargs["headers"]["Referer"] == "https://quote.eastmoney.com/"
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"data": {"total": 1, "diff": [{"f12": "600001"}]}})
    monkeypatch.setattr(provider, "_eastmoney_get", get)
    assert len(provider._recommendation_pages(scope, fields="f12,f14", max_pages=5)) == 1


def test_explicit_board_code_wins_over_ambiguous_name_cache(monkeypatch):
    from contextlib import nullcontext
    from src.services.screening_service import DsaEastMoneyHotspotProvider

    provider = DsaEastMoneyHotspotProvider()
    provider._recommendation_board_codes = {"同名主题": "BK9999"}
    monkeypatch.setattr(provider, "_source_call_budget", nullcontext)
    def pages(scope, **kwargs):
        assert scope == "b:BK0001 f:!50"
        return [{"f12": "600001", "f14": "样本"}]
    monkeypatch.setattr(provider, "_recommendation_pages", pages)
    result = provider.recommendation_constituents("同名主题", board_code="BK0001")
    assert result["board_code"] == "BK0001"
    with pytest.raises(ValueError, match="Invalid exact board"):
        provider.recommendation_constituents("同名主题", board_code="BK0001 injected")


def test_empty_recommendation_directory_reuses_hotspot_observation_source():
    class Service:
        def recommendation_hotspots(self, **kwargs):
            return {"hotspots": [], "source_errors": ["direct directory failed"]}

        def hotspots(self, **kwargs):
            return {"hotspots": [{"topic": "机器人", "change_pct": 2.0}]}

        def hotspot_detail(self, **kwargs):
            return {"canonical_topic": "机器人", "stocks": [{"code": "600001"}]}

    coverage = {}
    members, errors = hotspot_members(Service(), coverage)
    assert members["600001"]["topic"] == "机器人"
    assert coverage == {"hotspot_boards": 1, "hotspot_members": 1}
    assert any("同一来源" in error for error in errors)
