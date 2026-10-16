from datetime import date, timedelta

import pytest

from scripts.actions_report import render_screening
from scripts.actions_screening_report import technical_plan


def bars():
    start = date(2026, 9, 25)
    return [{"date": str(start + timedelta(days=i)), "high": 11, "low": 9, "close": 10}
            for i in range(21)]


def test_levels_use_actual_ranges_and_close_with_explicit_basis():
    plan = technical_plan(list(reversed(bars())), "test-source", date(2026, 10, 15))
    assert plan['entry'] == 10 and plan['atr20'] == 2
    assert plan['stop_loss'] == 9 and plan['take_profit'] == 12 and plan['target'] == 13
    content, code = render_screening({"ranking_mode": "llm", "candidates": [{
        "code": "600001", "reason": "test", "technical_plan": plan,
        "dsa_news": [{"title": "公告", "published_date": "2026-10-15", "source": "Bocha",
                      "url": "https://example.com/notice?q=1"}],
        "dsa_context": {"quote": {"price": 10, "source": "test", "provider_timestamp": "2026-10-15T15:00:00+08:00"}},
    }]}, requested=1)
    assert code == 0
    assert "目标价：13.00元" in content and "止损价：9.00元" in content
    assert "ATR20" in content and "2026-10-15T15:00:00+08:00" in content
    assert "[原文](<https://example.com/notice?q=1>)" in content


@pytest.mark.parametrize('mutation', ['missing', 'nan', 'inverted', 'duplicate', 'flat', 'short', 'stale', 'future'])
def test_unreliable_bars_never_produce_prices(mutation):
    rows = bars()
    if mutation == 'missing':
        del rows[-1]['high']
    elif mutation == 'nan':
        rows[-1]['high'] = float('nan')
    elif mutation == 'inverted':
        rows[-1]['low'] = 12
    elif mutation == 'duplicate':
        rows[-1]['date'] = rows[-2]['date']
    elif mutation == 'flat':
        for row in rows:
            row.update(high=10, low=10)
    elif mutation == 'short':
        rows.pop(0)
    elif mutation == 'stale':
        rows[-1]['date'] = '2026-10-14'
    elif mutation == 'future':
        rows[-1]['date'] = '2026-10-16'
    plan = technical_plan(rows, 'source', date(2026, 10, 15))
    assert plan['status'] == '无法测算'
    assert 'target' not in plan and 'stop_loss' not in plan


def test_missing_prices_and_shortfall_remain_visible_in_partial_report():
    content, code = render_screening({"ranking_mode": "llm", "candidates": [{"code": "600001"}]}, requested=10)
    assert code == 2 and '请求 10 只，实际返回 1 只' in content
    assert '目标价 / 止盈价 / 止损价：无法测算' in content
    assert '未取得可核验材料' in content


def test_intraday_bar_is_excluded_without_using_its_incomplete_ohlc():
    rows = bars() + [{"date": "2026-10-16", "high": None, "low": None, "close": 999}]
    plan = technical_plan(rows, "source", date(2026, 10, 15), incomplete_date=date(2026, 10, 16))
    assert plan["as_of"] == "2026-10-15" and plan["entry"] == 10
    assert plan["target"] == 13
    rows.append({"date": "2026-10-17", "high": 11, "low": 9, "close": 10})
    assert technical_plan(rows, "source", date(2026, 10, 15),
                          incomplete_date=date(2026, 10, 16))["status"] == "无法测算"


def test_complete_research_count_is_informational_but_partial_count_is_not():
    data = {"ranking_mode": "llm", "candidates": [{"code": "600001"}],
            "degradation": ['DSA provider context applied 12 of 12 candidates']}
    assert render_screening(data)[1] == 0
    data['degradation'] = ['DSA provider context applied 3 of 12 candidates']
    assert render_screening(data)[1] == 2


def test_undated_news_and_unsafe_link_cannot_be_presented_as_verified_catalyst():
    data = {"ranking_mode": "llm", "candidates": [{"code": "600001", "dsa_news": [
        {"title": '<script>notice</script>', "url": 'javascript:alert(1)'}]}]}
    content, _ = render_screening(data)
    assert '发布时间未核验' in content and '无有效原文链接' in content
    assert 'javascript:' not in content and '<script>' not in content
