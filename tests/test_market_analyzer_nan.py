"""Market review must never turn missing index data into nan or a bullish score."""

from src.market_analyzer import MarketAnalyzer, MarketIndex, MarketOverview
from src.core.market_profile import get_profile


def _nan_index(name="上证指数"):
    return MarketIndex(
        code="000001", name=name, current=float("nan"), change=0.0,
        change_pct=float("nan"), open=0.0, high=0.0, low=0.0,
        prev_close=0.0, volume=0.0, amount=0.0, amplitude=0.0,
    )


def _analyzer(monkeypatch):
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    analyzer.region = "cn"
    analyzer.profile = get_profile("cn")
    monkeypatch.setattr(analyzer, "_get_review_language", lambda: "zh")
    return analyzer


def test_nan_index_is_unavailable_not_bullish(monkeypatch):
    analyzer = _analyzer(monkeypatch)
    overview = MarketOverview(
        date="2026-11-25",
        indices=[_nan_index(), _nan_index("深证成指")],
    )
    scores = analyzer._build_market_light_scores(overview)
    assert scores["data_quality"] == "unavailable"

    block = analyzer._build_stats_block(overview)
    assert "数据不足" in block
    assert "68/100" not in block and "偏暖" not in block

    table = analyzer._build_indices_block(overview)
    assert "nan" not in table.lower()
    assert "数据缺失" in table
    assert "涨跌幅缺失" in table


def test_unknown_mood_text_is_explicit():
    analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
    assert analyzer._get_market_mood_text("unknown", "zh") == "数据不足，方向待确认"
