"""Deterministic universe, risk and concentration rules for daily recommendations."""

import math

import pandas as pd

from src.services.screening.normalize import normalize_code, safe_text


GROUPS = (
    ("high", "热点股票·高风险", "capital_heat"),
    ("medium", "热点股票·中风险", "momentum_quality"),
    ("low", "低风险股票", "low_volatility_quality"),
)


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def sector(row):
    name = safe_text(row.get("name"))
    industry = safe_text(row.get("industry"))
    return "银行" if "银行" in name or "银行" in industry else industry


def risk_band(row):
    values = [finite(row.get(key)) for key in (
        "atr_20_pct", "volatility_20d_pct", "max_drawdown_20d_pct",
        "change_pct", "turnover_rate", "daily_quality_score",
    )]
    if any(value is None for value in values):
        return ""
    atr, volatility, drawdown, change, turnover, quality = values
    flags = safe_text(row.get("daily_quality_flags"))
    if (quality < 80 or any(flag in flags for flag in (
            "stale", "fetch_failed", "invalid", "missing", "incomplete", "short_history"))
            or not 0 < atr <= 10 or not 0 <= volatility <= 100
            or not -25 <= drawdown <= 0 or abs(change) >= 9.5 or not 0 <= turnover <= 25):
        return ""
    if atr <= 2 and volatility <= 25 and drawdown >= -6 and abs(change) <= 3 and turnover <= 5:
        return "low"
    if atr <= 4 and volatility <= 45 and drawdown >= -12 and abs(change) <= 5 and turnover <= 10:
        return "medium"
    return "high"


def filter_universe(frame, policy):
    result = frame.copy()
    codes = result["code"].map(normalize_code)
    keep = ~codes.isin(policy.get("excluded_codes") or [])
    members = policy.get("members") or {}
    if policy["group"] != "low":
        keep &= codes.isin(members)
    result = result.loc[keep].copy()
    result["recommendation_topic"] = result["code"].map(
        lambda code: (members.get(normalize_code(code)) or {}).get("topic", ""))
    if "concepts" not in result:
        result["concepts"] = ""
    result["concepts"] = ["、".join(filter(None, [safe_text(concepts), topic]))
                          for concepts, topic in zip(result["concepts"], result["recommendation_topic"])]
    for key in ("board_heat_score", "industry_change_pct"):
        supplied = result["code"].map(lambda code: (members.get(normalize_code(code)) or {}).get(key))
        result[key] = supplied.combine_first(result[key]) if key in result else supplied
    return result


def filter_risk(frame, group):
    bands = frame.apply(risk_band, axis=1) if not frame.empty else pd.Series(dtype=str)
    # A calm hotspot still carries thematic risk; it belongs to the medium group.
    keep = bands.isin(["low", "medium"]) if group == "medium" else bands.eq(group)
    return frame.loc[keep].copy()


def diverse_rows(rows, limit, *, bank_budget=2, topic_limit=3, sector_limit=3):
    selected, seen, sectors, topics = [], set(), {}, {}
    banks = 0
    for row in rows:
        code = normalize_code(row.get("code"))
        industry = sector(row)
        topic = safe_text(row.get("recommendation_topic"))
        if not code or code in seen:
            continue
        if industry == "银行" and banks >= bank_budget:
            continue
        if industry and sectors.get(industry, 0) >= sector_limit:
            continue
        if topic and topics.get(topic, 0) >= topic_limit:
            continue
        selected.append(row)
        seen.add(code)
        sectors[industry] = sectors.get(industry, 0) + 1
        topics[topic] = topics.get(topic, 0) + 1
        banks += industry == "银行"
        if len(selected) >= limit:
            break
    return selected


def diverse_frame(frame, limit, policy, *, research=False):
    rows = frame.to_dict("records")
    selected = diverse_rows(
        rows, limit, bank_budget=policy.get("bank_budget", 2),
        topic_limit=10 if research else 3, sector_limit=10 if research else 3,
    )
    codes = {normalize_code(row["code"]) for row in selected}
    return frame.loc[frame["code"].map(normalize_code).isin(codes)].head(limit).copy()
