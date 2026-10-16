"""Evidence and conditional technical levels for Actions screening reports."""

from datetime import date, datetime
import math
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def technical_plan(rows: list[dict], source: str, expected_date: date,
                   *, incomplete_date: date | None = None) -> dict:
    """Use completed daily bars only; never fill missing OHLC or stale dates."""
    missing = {"status": "无法测算", "reason": "日线不足21条，或日期/OHLC无效"}
    bars = []
    try:
        for row in rows:
            day = date.fromisoformat(str(row["date"])[:10])
            if day == incomplete_date and day > expected_date:
                continue
            values = [number(row.get(key)) for key in ("high", "low", "close")]
            if any(value is None or value <= 0 for value in values):
                return missing
            high, low, close = values
            if not low <= close <= high:
                return missing
            bars.append((day, high, low, close))
    except (KeyError, TypeError, ValueError):
        return missing
    bars.sort()
    if len(bars) < 21 or len({bar[0] for bar in bars}) != len(bars):
        return missing
    last = bars[-1][0]
    if last != expected_date:
        return {"status": "无法测算", "reason": f"最新日线{last}与应有完整交易日{expected_date}不一致"}
    window = bars[-20:]
    previous = bars[-21:-1]
    atr = sum(max(high - low, abs(high - prev[3]), abs(low - prev[3]))
              for (_, high, low, _), prev in zip(window, previous)) / 20
    entry = window[-1][3]
    support = min(bar[2] for bar in window)
    resistance = max(bar[1] for bar in window)
    stop = round(max(support, entry - 2 * atr), 2)
    risk = entry - stop
    profit, target = round(entry + 2 * risk, 2), round(entry + 3 * risk, 2)
    if not 0 < stop < entry < profit < target or atr <= 0:
        return {"status": "无法测算", "reason": "波动不足或支撑与参考价重合，无法建立有效风险区间"}
    return {
        "status": "条件式技术测算", "as_of": str(last), "source": source,
        "entry": round(entry, 2), "stop_loss": stop, "take_profit": profit, "target": target,
        "atr20": round(atr, 4), "ma5": round(sum(bar[3] for bar in window[-5:]) / 5, 2),
        "ma20": round(sum(bar[3] for bar in window) / 20, 2),
        "support20": round(support, 2), "resistance20": round(resistance, 2),
    }


def attach_technical_plans(data: dict) -> None:
    from src.core.trading_calendar import get_effective_trading_date
    from src.services.screening_service import get_dsa_daily_history

    expected = get_effective_trading_date("cn")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for candidate in data.get("candidates") or []:
        try:
            frame, source = get_dsa_daily_history(candidate["code"], lookback_days=90)
            rows = frame.to_dict("records") if frame is not None else []
            candidate["technical_plan"] = technical_plan(
                rows, source, expected, incomplete_date=today if expected < today else None,
            )
        except Exception as exc:
            candidate["technical_plan"] = {"status": "无法测算", "reason": f"日线读取失败：{type(exc).__name__}"}


def evidence_lines(candidate: dict, cell, today: date) -> list[str]:
    lines, seen = [], set()
    context = candidate.get("dsa_context") or {}
    for key, label in (("dsa_news", "新闻"), ("dsa_events", "公告/事件")):
        context_key = "news" if key == "dsa_news" else "events"
        items = candidate.get(key) or (context.get(context_key) or {}).get("results") or []
        for item in items[:3]:
            title, url = item.get("title") or item.get("snippet"), str(item.get("url") or "")
            if not title or (title, url) in seen:
                continue
            seen.add((title, url))
            published = item.get("published_date")
            try:
                age = (today - date.fromisoformat(str(published)[:10])).days
                freshness = "近7日" if 0 <= age <= 7 else "背景资料，非近期催化"
            except ValueError:
                freshness = "发布时间未核验，不能视为近期催化"
            parsed = urlsplit(url)
            link = f"[原文](<{quote(url, safe=':/?=&%#@+~.-_')}>)" if parsed.scheme in {"http", "https"} and parsed.netloc else "无有效原文链接"
            lines.append(f"- {label}：{cell(title)}；{cell(published)}；{freshness}；{cell(item.get('source'))}；{link}")
            if item.get("snippet"):
                lines.append(f"  摘要：{cell(str(item['snippet'])[:400])}")
    return lines or ["- 新闻/事件：未取得可核验材料，不能判断消息利好或利空。"]


def candidate_details(candidate: dict, index: int, cell, today: date) -> list[str]:
    context = candidate.get("dsa_context") or {}
    quote_data = context.get("quote") or {}
    plan = candidate.get("technical_plan") or {"status": "无法测算", "reason": "未取得日线"}
    lines = ["", f"## {index}. {cell(candidate.get('name'))}（{cell(candidate.get('code'))}）", "",
             f"- 行业：{cell(candidate.get('industry') or candidate.get('llm_sector'))}；分数：{cell(candidate.get('score'))}（非收益预测）。"]
    explanations = candidate.get("why_selected") or []
    if explanations:
        lines.extend(f"- 入选理由：{cell(item.get('text'))}（{cell(item.get('source'))} / {cell(item.get('quality'))}）" for item in explanations)
    else:
        lines.append(f"- 入选理由：{cell(candidate.get('reason'))}")
    if quote_data:
        lines.extend([
            f"- 报价：{cell(quote_data.get('price'))}元；涨跌幅：{cell(quote_data.get('change_pct'))}%；成交额：{cell(quote_data.get('amount'))}元；量比：{cell(quote_data.get('volume_ratio'))}；换手率：{cell(quote_data.get('turnover_rate'))}%。",
            f"- 报价来源：{cell(quote_data.get('source'))}；行情时间：{cell(quote_data.get('provider_timestamp'))}；获取时间：{cell(quote_data.get('fetched_at'))}；质量：{cell(quote_data.get('data_quality'))}；过期标记：{cell(quote_data.get('is_stale'))}。行情时间缺失时不能确认是当日实时行情。",
        ])
    else:
        lines.append(f"- 筛选快照价：{cell(candidate.get('price'))}元；涨跌幅：{cell(candidate.get('change_pct'))}%；成交额：{cell(candidate.get('amount'))}元。未取得带时间的独立报价，不能视为实时行情。")
    if plan["status"] == "条件式技术测算":
        lines.extend([
            f"- 日线依据：{cell(plan['source'])}，截至{cell(plan['as_of'])}；MA5 / MA20：{plan['ma5']} / {plan['ma20']}元；20日支撑 / 压力：{plan['support20']} / {plan['resistance20']}元；ATR20：{plan['atr20']}元。",
            f"- 参考入场价：{plan['entry']:.2f}元；目标价：{plan['target']:.2f}元；止盈价：{plan['take_profit']:.2f}元；止损价：{plan['stop_loss']:.2f}元。",
            "- 价格依据：盘中剔除当天未收盘日线，以最新完整日线收盘价为入场假设；止损取20日最低价与入场价减2×ATR20中的较高值；风险R=入场价−止损价；止盈=入场价+2R，技术目标=入场价+3R。观察期5–20个交易日，属于条件式技术测算。实际成交价变化、除权或突发新闻后须重新计算；跳空时止损价不保证成交。",
        ])
    else:
        lines.append(f"- 目标价 / 止盈价 / 止损价：无法测算。原因：{cell(plan.get('reason'))}。")
    lines.extend(evidence_lines(candidate, cell, today))
    for key, label in (("llm_catalysts", "潜在催化（模型推断）"), ("llm_risks", "风险（模型推断）"),
                       ("llm_invalidators", "逻辑失效条件（模型推断）"), ("llm_watch_items", "需继续观察")):
        values = candidate.get(key) or []
        if values:
            lines.append(f"- {label}：{cell('；'.join(map(str, values)) if isinstance(values, list) else values)}")
    lines.append(f"- 风险提示：{cell(candidate.get('risk_summary') or candidate.get('risk_flags') or '未提供，不代表没有风险')}。新闻标题与股价涨跌不能直接证明因果关系。")
    lines.extend(f"- 数据问题：{cell(warning)}" for warning in context.get("warnings") or [])
    return lines
