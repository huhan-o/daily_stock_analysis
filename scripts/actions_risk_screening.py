"""Build three independent research groups using the shared screening engine."""

from src.services.screening.normalize import normalize_code, safe_text
from src.services.screening.recommendations import GROUPS, diverse_rows, finite, sector


def hotspot_members(service):
    payload = service.hotspots(top=12, refresh=True, include_details=False)
    members, errors = {}, list(payload.get("source_errors") or [])
    if payload.get("stale") or payload.get("cache_used"):
        return {}, [*errors, "热点快照过期或使用缓存，未用于热点推荐。"]
    for row in payload.get("hotspots") or []:
        topic = safe_text(row.get("canonical_topic") or row.get("topic") or row.get("name"))
        change = finite(row.get("change_pct"))
        if not topic or change is None or change <= 0:
            continue
        try:
            detail = service.hotspot_detail(topic=topic, refresh=True, include_search=False)
        except Exception as exc:
            errors.append(f"热点成分股读取失败：{topic}（{type(exc).__name__}）")
            continue
        if detail.get("stale") or detail.get("quality_status") in {"failed", "stale"}:
            errors.append(f"热点成分股不可核验：{topic}")
            continue
        canonical = safe_text(detail.get("canonical_topic"))
        if canonical and canonical != topic:
            errors.append(f"热点名称解析不一致：{topic} / {canonical}，未采用成分股。")
            continue
        errors.extend(detail.get("source_errors") or [])
        for stock in detail.get("stocks") or []:
            code = normalize_code(stock.get("code"))
            if (not code or stock.get("fallback_used") or stock.get("stale")
                    or "related" in safe_text(stock.get("source")).lower()):
                continue
            members.setdefault(code, {
                "topic": topic, "board_heat_score": finite(row.get("heat_score")),
                "industry_change_pct": change,
                "source": stock.get("source") or detail.get("provider") or payload.get("provider_used"),
            })
    if not members:
        errors.append("未取得可核验的上涨热点及其成分股，高、中风险组不以非热点股票补数。")
    return members, errors


def recommendation_reason(item):
    raw = item.get("raw") or {}
    atr = finite(raw.get("atr_20_pct"))
    volatility = finite(raw.get("volatility_20d_pct"))
    drawdown = finite(raw.get("max_drawdown_20d_pct"))
    if any(value is None for value in (atr, volatility, drawdown)):
        return ""
    topic = item.get("recommendation_topic")
    basis = f"{topic}上涨热点成分股" if topic else "低波动质量策略入围"
    risk = f"ATR {atr:.1f}%、20日年化波动率 {volatility:.1f}%、回撤 {drawdown:.1f}%"
    thesis = safe_text(item.get("llm_thesis"))
    return f"{basis}；{risk}。" + (f"模型判断：{thesis[:100]}" if thesis else "依据量价和波动规则筛选，模型理由缺失。")


def build_recommendations(service, count, attach_plans, on_group=None):
    result = {"groups": [], "warnings": [], "candidates": [], "requested": count * 3}
    if on_group is not None:
        on_group(result)
    try:
        members, errors = hotspot_members(service)
    except Exception as exc:
        members, errors = {}, [f"热点读取失败：{type(exc).__name__}，仍尝试低风险组。"]
    result["warnings"] = errors
    used, banks = set(), 0
    for group, title, strategy in GROUPS:
        data = {"candidates": [], "ranking_mode": "factor", "warnings": []}
        try:
            if group == "low" or members:
                data = service.screen(
                    strategy=strategy, market="cn", max_results=12, research_limit=12,
                    recommendation_policy={"group": group, "members": members,
                                           "excluded_codes": sorted(used), "bank_budget": max(2 - banks, 0)},
                )
                attach_plans(data)
        except Exception as exc:
            data.setdefault("warnings", []).append(f"{title}筛选失败：{type(exc).__name__}")
        eligible = []
        for item in data.get("candidates") or []:
            code = normalize_code(item.get("code"))
            if code in used or (group != "low" and code not in members):
                continue
            item["code"] = code
            item["recommendation_topic"] = (members.get(code) or {}).get("topic", "")
            item["recommendation_source"] = (members.get(code) or {}).get("source", "")
            item["recommendation_group"] = group
            item["recommendation_reason"] = recommendation_reason(item)
            plan = item.get("technical_plan") or {}
            levels = [finite(plan.get(key)) for key in ("stop_loss", "entry", "take_profit")]
            if (not item["recommendation_reason"] or plan.get("status") != "条件式技术测算"
                    or any(value is None for value in levels) or not 0 < levels[0] < levels[1] < levels[2]):
                continue
            eligible.append(item)
        selected = diverse_rows(eligible, count, bank_budget=max(2 - banks, 0))
        for item in selected:
            used.add(item["code"])
            banks += sector(item) == "银行"
        data.update(group=group, title=title, requested=count, candidates=selected)
        result["groups"].append(data)
        result["candidates"].extend(selected)
        if on_group is not None:
            on_group(result)
    return result
