from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import ClassificationConfidence, ControlLevel, VariableClassification

RULE_VERSION = "2026.10.06-v1"


@dataclass(frozen=True)
class Rule:
    level: ControlLevel
    dimension: str
    aliases: tuple[str, ...]


RULES = (
    Rule(
        ControlLevel.firm,
        "size",
        ("size", "lnsize", "asset", "assets", "totalasset", "企业规模", "公司规模", "总资产"),
    ),
    Rule(
        ControlLevel.firm,
        "age",
        ("firmage", "companyage", "listage", "age", "企业年龄", "公司年龄", "上市年限", "成立年限"),
    ),
    Rule(
        ControlLevel.firm,
        "ownership",
        ("soe", "stateowned", "ownership", "private", "国有", "所有制", "产权性质", "民营"),
    ),
    Rule(
        ControlLevel.firm,
        "profitability",
        (
            "roa",
            "roe",
            "profit",
            "margin",
            "returnonasset",
            "盈利",
            "利润",
            "资产收益率",
            "净资产收益率",
        ),
    ),
    Rule(
        ControlLevel.firm,
        "capital_structure",
        ("lev", "leverage", "debt", "assetliability", "资本结构", "资产负债", "杠杆"),
    ),
    Rule(
        ControlLevel.firm,
        "growth",
        ("growth", "grow", "salesgrowth", "revenuegrowth", "成长", "增长率", "营收增长"),
    ),
    Rule(
        ControlLevel.firm,
        "liquidity_cashflow",
        (
            "cashflow",
            "cash",
            "liquidity",
            "currentratio",
            "quick",
            "现金流",
            "流动比率",
            "速动比率",
        ),
    ),
    Rule(
        ControlLevel.firm,
        "governance",
        (
            "board",
            "director",
            "dual",
            "independent",
            "top1",
            "governance",
            "董事会",
            "独立董事",
            "两职合一",
            "股权集中",
        ),
    ),
    Rule(
        ControlLevel.firm,
        "innovation",
        ("rd", "rnd", "patent", "innovation", "研发", "专利", "创新"),
    ),
    Rule(
        ControlLevel.firm,
        "financing_constraints",
        ("financingconstraint", "constraint", "kzindex", "wwindex", "saindex", "融资约束"),
    ),
    Rule(
        ControlLevel.firm,
        "factor_intensity",
        ("labor", "capitalintensity", "employee", "劳动密集", "资本密集", "员工人数"),
    ),
    Rule(ControlLevel.firm, "risk", ("risk", "volatility", "zscore", "风险", "波动率")),
    Rule(
        ControlLevel.regional,
        "economic_development",
        (
            "gdp",
            "pgdp",
            "percapitagdp",
            "economicdevelopment",
            "地区生产总值",
            "人均gdp",
            "经济发展",
        ),
    ),
    Rule(
        ControlLevel.regional,
        "population_urbanization",
        ("population", "urbanization", "density", "人口", "城镇化", "人口密度"),
    ),
    Rule(
        ControlLevel.regional,
        "industrial_structure",
        (
            "industrialstructure",
            "secondaryindustry",
            "tertiaryindustry",
            "产业结构",
            "第二产业",
            "第三产业",
        ),
    ),
    Rule(
        ControlLevel.regional,
        "fiscal_government",
        ("fiscal", "government", "govexpenditure", "财政", "政府干预", "政府支出"),
    ),
    Rule(
        ControlLevel.regional,
        "financial_development",
        ("financialdevelopment", "bankloan", "credit", "金融发展", "银行贷款", "信贷"),
    ),
    Rule(
        ControlLevel.regional,
        "infrastructure",
        ("infrastructure", "road", "railway", "基础设施", "道路", "铁路"),
    ),
    Rule(
        ControlLevel.regional,
        "human_capital",
        ("humancapital", "education", "college", "人力资本", "教育水平", "高校"),
    ),
    Rule(
        ControlLevel.regional,
        "openness",
        ("fdi", "openness", "foreigninvestment", "trade", "开放程度", "外商投资", "进出口"),
    ),
    Rule(
        ControlLevel.regional,
        "environment",
        ("pollution", "pm25", "emission", "green", "环境", "污染", "排放", "绿色"),
    ),
    Rule(
        ControlLevel.regional,
        "digitalization",
        ("digital", "broadband", "internet", "数字经济", "数字化", "宽带", "互联网"),
    ),
)


def normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def classify_variable(name: str, label: str) -> VariableClassification:
    normalized_name = normalize(name)
    normalized_label = normalize(label)
    comparable_names = {normalized_name}
    if normalized_name.startswith("ln") and len(normalized_name) > 2:
        comparable_names.add(normalized_name[2:])
    exact: list[tuple[Rule, str]] = []
    partial: list[tuple[Rule, str, str]] = []
    for rule in RULES:
        for alias in rule.aliases:
            token = normalize(alias)
            if token in comparable_names:
                exact.append((rule, alias))
            elif token and token in normalized_name:
                partial.append((rule, alias, "name"))
            elif token and token in normalized_label:
                partial.append((rule, alias, "label"))
    matches = exact or [(rule, alias) for rule, alias, _ in partial]
    if not matches:
        return VariableClassification(
            variable=name,
            suggested_level=ControlLevel.other,
            suggested_dimension="unclassified",
            level=ControlLevel.other,
            dimension="unclassified",
            confidence=ClassificationConfidence.unrecognized,
            reason="No dictionary term matched",
        )
    rule, alias = matches[0]
    distinct = {(item[0].level, item[0].dimension) for item in matches}
    if exact:
        confidence = (
            ClassificationConfidence.high if len(distinct) == 1 else ClassificationConfidence.low
        )
        reason = f"Exact variable-name match: {alias}"
    else:
        source = next(
            source for item, item_alias, source in partial if item == rule and item_alias == alias
        )
        confidence = (
            ClassificationConfidence.medium if len(distinct) == 1 else ClassificationConfidence.low
        )
        reason = f"{source.capitalize()} matched dictionary term: {alias}"
    if len(distinct) > 1:
        reason += "; multiple dimensions matched, review required"
    return VariableClassification(
        variable=name,
        suggested_level=rule.level,
        suggested_dimension=rule.dimension,
        level=rule.level,
        dimension=rule.dimension,
        confidence=confidence,
        reason=reason,
    )


def classify_payload(variables: list[dict[str, Any]]) -> dict[str, Any]:
    classifications = [
        classify_variable(item["name"], item.get("label", ""))
        for item in variables
        if item.get("numeric")
    ]
    return {
        "rule_version": RULE_VERSION,
        "classifications": [item.model_dump(mode="json") for item in classifications],
    }
