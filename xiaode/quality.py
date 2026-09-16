from __future__ import annotations

import json
import re
import unicodedata
from collections import OrderedDict

from .models import (
    AuditDecision,
    AuditReport,
    CoverageItem,
    EffectiveAudit,
    EvidenceItem,
    EvidenceLedger,
    QualityResult,
    ReportDraft,
    ReportStatement,
    ReportValidation,
    SourceRecord,
)
from .security import canonical_url


DIMENSIONS = OrderedDict(
    {
        "product": {
            "label": "产品与能力",
            "triggers": ("产品", "功能", "能力", "体验", "定位", "product", "feature"),
            "terms": ("产品功能", "产品更新", "功能", "产品", "能力", "定位", "feature"),
            "types": {"产品功能", "产品更新"},
        },
        "users_market": {
            "label": "用户与市场",
            "triggers": ("用户", "市场", "份额", "客户", "mau", "dau", "market", "user"),
            "terms": ("用户", "市场", "份额", "客户", "活跃", "mau", "dau", "market"),
            "types": {"用户数据", "市场数据"},
        },
        "business": {
            "label": "商业模式",
            "triggers": ("商业", "收入", "营收", "定价", "订阅", "变现", "revenue", "pricing"),
            "terms": ("商业", "收入", "营收", "定价", "订阅", "付费", "revenue", "pricing"),
            "types": {"商业模式", "价格"},
        },
        "growth": {
            "label": "增长与分发",
            "triggers": ("增长", "分发", "渠道", "获客", "留存", "合作", "growth", "distribution"),
            "terms": ("增长", "分发", "渠道", "获客", "留存", "合作", "growth", "partner"),
            "types": {"增长", "渠道", "合作"},
        },
        "competition": {
            "label": "竞争与威胁",
            "triggers": ("竞争", "竞品", "威胁", "替代", "搜索", "competition", "threat"),
            "terms": ("竞争", "竞品", "威胁", "替代", "搜索", "competition", "search"),
            "types": {"竞争", "公司战略"},
        },
        "technology": {
            "label": "技术与生态",
            "triggers": ("技术", "模型", "api", "生态", "集成", "开发者", "technology", "ecosystem"),
            "terms": ("技术", "模型", "api", "生态", "集成", "连接器", "developer", "model"),
            "types": {"技术能力"},
        },
        "china": {
            "label": "中国市场与本土竞争",
            "triggers": ("中国", "国内", "百度", "文心", "豆包", "kimi", "夸克", "china", "baidu"),
            "terms": ("中国", "国内", "百度", "文心", "豆包", "kimi", "夸克", "china", "baidu"),
            "types": {"竞争", "市场数据"},
        },
    }
)


REPORT_SECTIONS = OrderedDict(
    {
        "0": "Executive Summary",
        "1": "一句话判断",
        "2": "产品定位",
        "3": "核心用户与场景",
        "4": "核心产品能力",
        "5": "最近半年产品变化",
        "6": "用户与市场表现",
        "7": "商业模式",
        "8": "增长逻辑",
        "9": "核心竞争壁垒",
        "10": "主要问题与风险",
        "11": "对用户指定业务的威胁",
        "12": "值得借鉴的策略",
        "13": "最终判断",
        "14": "研究缺口",
    }
)

REPORT_LABELS = {
    "FACT": "【事实】",
    "ANALYSIS": "【判断】",
    "RECOMMENDATION": "【建议】",
    "HYPOTHESIS": "【待验证假设】",
    "GAP": "【研究缺口】",
}


def requested_dimensions(focus: str) -> list[str]:
    folded = focus.casefold().strip()
    if not folded or any(word in folded for word in ("完整", "全面", "全量", "full")):
        return [key for key in DIMENSIONS if key != "china"]
    selected = ["product"]
    for key, config in DIMENSIONS.items():
        if key == "product":
            continue
        if any(trigger.casefold() in folded for trigger in config["triggers"]):
            selected.append(key)
    return list(dict.fromkeys(selected))


def _normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.translate(
        str.maketrans(
            {
                "“": '"',
                "”": '"',
                "‘": "'",
                "’": "'",
                "–": "-",
                "—": "-",
                "\u00a0": " ",
            }
        )
    )
    return re.sub(r"\s+", " ", value).strip()


def validate_evidence_ledger(
    ledger: EvidenceLedger,
    sources: list[SourceRecord],
) -> dict[str, list[str]]:
    source_map = {source.source_id: source for source in sources}
    errors: dict[str, list[str]] = {}
    expected_ids = [f"E{index:02d}" for index in range(1, len(ledger.evidence) + 1)]
    actual_ids = [item.evidence_id for item in ledger.evidence]

    for position, evidence in enumerate(ledger.evidence):
        item_errors: list[str] = []
        if position < len(expected_ids) and evidence.evidence_id != expected_ids[position]:
            item_errors.append(f"证据编号应为 {expected_ids[position]}")
        source = source_map.get(evidence.source_id)
        if source is None:
            item_errors.append("Source ID 不存在")
        else:
            try:
                same_url = canonical_url(evidence.url) == canonical_url(source.url)
            except ValueError:
                same_url = False
            if not same_url:
                item_errors.append("URL 与 Source ID 对应的原始来源不一致")
            if evidence.source_tier != source.source_tier:
                item_errors.append("Source Tier 与程序保存的来源等级不一致")
            if evidence.published_date != source.published_date:
                item_errors.append("Published Date 与程序保存的原始来源不一致")
            quote = _normalized_text(evidence.evidence_text)
            body = _normalized_text(source.content)
            if quote not in body:
                item_errors.append("Evidence Text 不是原始正文的逐字片段")
        if len(_normalized_text(evidence.evidence_text)) < 8:
            item_errors.append("Evidence Text 过短，无法独立核验")
        if evidence.source_tier == "Tier D":
            item_errors.append("Tier D 来源禁止进入证据池")
        if evidence.source_tier == "Tier C" and evidence.confidence == "High":
            item_errors.append("Tier C 来源的置信度不能标记为 High")
        if evidence.published_date in {"", "未知", "未确认"} and evidence.confidence == "High":
            item_errors.append("发布日期未知时置信度不能标记为 High")
        if item_errors:
            errors[evidence.evidence_id] = item_errors

    if actual_ids != expected_ids and not ledger.evidence:
        errors["__ledger__"] = ["Evidence Ledger 为空"]
    return errors


def build_audit_source_context(
    ledger: EvidenceLedger,
    sources: list[SourceRecord],
    *,
    radius: int = 1800,
) -> list[dict[str, str]]:
    source_map = {source.source_id: source for source in sources}
    contexts: list[dict[str, str]] = []
    for evidence in ledger.evidence:
        source = source_map.get(evidence.source_id)
        if source is None:
            continue
        index = source.content.find(evidence.evidence_text)
        if index < 0:
            excerpt = source.content
        else:
            start = max(0, index - radius)
            end = min(len(source.content), index + len(evidence.evidence_text) + radius)
            excerpt = source.content[start:end]
        contexts.append(
            {
                "evidence_id": evidence.evidence_id,
                "source_id": source.source_id,
                "title": source.title,
                "url": source.url,
                "source_tier": source.source_tier,
                "published_date": source.published_date,
                "raw_context": excerpt,
            }
        )
    return contexts


def _rejected_decision(evidence: EvidenceItem, reason: str) -> AuditDecision:
    return AuditDecision(
        evidence_id=evidence.evidence_id,
        status="REJECTED",
        approved_claim=evidence.claim,
        claim_nature=evidence.claim_nature,
        source_id=evidence.source_id,
        source_tier=evidence.source_tier,
        confidence=evidence.confidence,
        required_qualification="",
        audit_reason=reason,
    )


def _evidence_coverage(
    ledger: EvidenceLedger,
    allowed_ids: set[str],
    focus: str,
) -> list[CoverageItem]:
    result: list[CoverageItem] = []
    for key in requested_dimensions(focus):
        config = DIMENSIONS[key]
        matches: list[str] = []
        for item in ledger.evidence:
            if item.evidence_id not in allowed_ids:
                continue
            blob = _normalized_text(f"{item.evidence_type} {item.claim} {item.evidence_text}")
            type_match = key != "china" and item.evidence_type in config["types"]
            if type_match or any(
                _normalized_text(term) in blob for term in config["terms"]
            ):
                matches.append(item.evidence_id)
        unique = list(dict.fromkeys(matches))
        result.append(
            CoverageItem(
                dimension=key,
                label=config["label"],
                required=1,
                evidence_ids=unique,
                covered=len(unique) >= 1,
            )
        )
    return result


def apply_quality_gate(
    ledger: EvidenceLedger,
    audit: AuditReport,
    precheck_errors: dict[str, list[str]],
    focus: str,
) -> QualityResult:
    audit_map = {decision.evidence_id: decision for decision in audit.decisions}
    evidence_ids = {item.evidence_id for item in ledger.evidence}
    unreviewed: list[str] = []
    effective: list[AuditDecision] = []
    programmatic_risks: list[str] = []

    extra_audits = sorted(set(audit_map) - evidence_ids)
    if extra_audits:
        programmatic_risks.append(f"Auditor 返回了不存在的证据编号：{', '.join(extra_audits)}")

    for evidence in ledger.evidence:
        decision = audit_map.get(evidence.evidence_id)
        if decision is None:
            unreviewed.append(evidence.evidence_id)
            effective.append(_rejected_decision(evidence, "Auditor 未审核该证据；代码按 fail-closed 拒绝。"))
            continue

        consistency_errors: list[str] = list(precheck_errors.get(evidence.evidence_id, []))
        if decision.approved_claim != evidence.claim:
            consistency_errors.append("Auditor 改写了 Claim，不能绕过原始证据边界")
        if decision.source_id != evidence.source_id:
            consistency_errors.append("Auditor 的 Source ID 与 Evidence 不一致")
        if decision.source_tier != evidence.source_tier:
            consistency_errors.append("Auditor 的 Source Tier 与 Evidence 不一致")
        if decision.claim_nature != evidence.claim_nature:
            consistency_errors.append("Auditor 的 Claim Nature 与 Evidence 不一致")
        confidence_rank = {"Low": 0, "Medium": 1, "High": 2}
        if confidence_rank[decision.confidence] > confidence_rank[evidence.confidence]:
            consistency_errors.append("Auditor 不得提升 Extractor 给出的置信度")
        if decision.status == "CAUTION" and decision.required_qualification.casefold() in {
            "",
            "无",
            "none",
            "n/a",
        }:
            consistency_errors.append("CAUTION 缺少必须保留的限定语")

        if consistency_errors:
            reason = "代码硬门禁拒绝：" + "；".join(consistency_errors)
            effective.append(_rejected_decision(evidence, reason))
            programmatic_risks.append(f"{evidence.evidence_id}: {reason}")
        else:
            effective.append(decision)

    approved_ids = [item.evidence_id for item in effective if item.status == "APPROVED"]
    caution_ids = [item.evidence_id for item in effective if item.status == "CAUTION"]
    rejected_ids = [item.evidence_id for item in effective if item.status == "REJECTED"]
    total = len(ledger.evidence)
    approved_rate = round(len(approved_ids) / total, 4) if total else 0.0
    usable_rate = round((len(approved_ids) + len(caution_ids)) / total, 4) if total else 0.0
    rejected_rate = len(rejected_ids) / total if total else 1.0
    caution_rate = len(caution_ids) / total if total else 1.0
    if total and approved_rate >= 0.8 and rejected_rate <= 0.1 and caution_rate <= 0.2:
        overall = "HIGH"
    elif total and usable_rate >= 0.6:
        overall = "MEDIUM"
    else:
        overall = "LOW"

    effective_audit = EffectiveAudit(
        decisions=effective,
        risks=list(dict.fromkeys([*audit.risks, *programmatic_risks])),
        approved_ids=approved_ids,
        caution_ids=caution_ids,
        rejected_ids=rejected_ids,
        unreviewed_ids=unreviewed,
        total_evidence=total,
        approved_rate=approved_rate,
        usable_rate=usable_rate,
        overall_quality=overall,
    )
    allowed_ids = set(approved_ids + caution_ids)
    coverage = _evidence_coverage(ledger, allowed_ids, focus)
    gaps = list(ledger.missing_evidence)
    gaps.extend(f"{item.label}缺少通过审核的直接证据" for item in coverage if not item.covered)

    evidence_map = {item.evidence_id: item for item in ledger.evidence}
    decision_map = {item.evidence_id: item for item in effective}
    allowed_payload = []
    for evidence_id in approved_ids + caution_ids:
        evidence = evidence_map[evidence_id]
        decision = decision_map[evidence_id]
        allowed_payload.append(
            {
                "evidence_id": evidence.evidence_id,
                "audit_status": decision.status,
                "claim": evidence.claim,
                "claim_nature": evidence.claim_nature,
                "source_id": evidence.source_id,
                "source_name": evidence.source_name,
                "source_tier": evidence.source_tier,
                "url": evidence.url,
                "published_date": evidence.published_date,
                "evidence_text": evidence.evidence_text,
                "confidence": decision.confidence,
                "required_qualification": decision.required_qualification,
            }
        )
    packet = json.dumps(
        {
            "allowed_evidence": allowed_payload,
            "research_gaps": list(dict.fromkeys(gaps)),
            "hard_rules": [
                "只能使用 allowed_evidence 中的事实。",
                "CAUTION 必须逐字保留 required_qualification 的不确定性。",
                "每句事实和判断都必须引用一个或多个 [E##]。",
                "分析必须标注为【判断】，行动建议必须标注为【建议】。",
                "证据不足必须写入研究缺口，禁止用模型记忆补全。",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    return QualityResult(
        audit=effective_audit,
        precheck_errors=precheck_errors,
        evidence_coverage=coverage,
        writer_packet=packet,
    )


_CITATION = re.compile(r"\[(E\d{2,})\]")
_BARE_EVIDENCE = re.compile(r"(?<!\[)\b(E\d{2,})\b(?!\])")
_REPORT_LABEL_PREFIX = re.compile(
    r"^\s*(?:[-*]\s*)?(?:【(?:事实|判断|建议|待验证假设|研究缺口)】\s*)+"
)
_STRONG_FACT = re.compile(
    r"(?:\b20\d{2}\b|[$¥€£]\s*\d|\d+(?:\.\d+)?\s*(?:%|％|万|亿|million|billion|美元|元|月|年))",
    re.IGNORECASE,
)


def _clean_report_text(value: str) -> str:
    text = value.replace("\r", " ").replace("\n", " ")
    text = _REPORT_LABEL_PREFIX.sub("", text)
    text = _CITATION.sub("", text)
    text = _BARE_EVIDENCE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -*#")
    return text


def render_report_draft(
    draft: ReportDraft,
    ledger: EvidenceLedger,
    audit: EffectiveAudit,
) -> tuple[str, list[str]]:
    """Normalize a structured draft and render citation-safe Markdown deterministically."""

    allowed_order = audit.approved_ids + audit.caution_ids
    allowed_ids = set(allowed_order)
    evidence_map = {
        item.evidence_id: item for item in ledger.evidence if item.evidence_id in allowed_ids
    }
    decision_map = {
        item.evidence_id: item for item in audit.decisions if item.evidence_id in allowed_ids
    }
    fallback_id = next((item for item in allowed_order if item in evidence_map), None)
    if fallback_id is None:
        raise ValueError("没有可用于生成报告的通过门禁证据")

    adjustments: list[str] = []
    raw_sections: dict[str, list[ReportStatement]] = {
        section_id: [] for section_id in REPORT_SECTIONS
    }
    seen_sections: set[str] = set()
    for section in draft.sections:
        if section.section_id in seen_sections:
            adjustments.append(f"合并重复章节 {section.section_id}")
        seen_sections.add(section.section_id)
        raw_sections[section.section_id].extend(section.statements)

    normalized: dict[str, list[tuple[str, str, list[str]]]] = {
        section_id: [] for section_id in REPORT_SECTIONS
    }
    for section_id, title in REPORT_SECTIONS.items():
        for statement in raw_sections[section_id]:
            valid_ids = list(
                dict.fromkeys(
                    evidence_id
                    for evidence_id in statement.evidence_ids
                    if evidence_id in allowed_ids and evidence_id in evidence_map
                )
            )
            invalid_ids = sorted(set(statement.evidence_ids) - set(valid_ids))
            if invalid_ids:
                adjustments.append(
                    f"章节 {section_id} 移除未通过门禁的证据：{', '.join(invalid_ids)}"
                )

            text = _clean_report_text(statement.text)
            if statement.kind == "FACT":
                if not valid_ids:
                    adjustments.append(f"章节 {section_id} 删除无有效引用的事实")
                    continue
                for evidence_id in valid_ids:
                    claim = _clean_report_text(evidence_map[evidence_id].claim)
                    if claim:
                        normalized[section_id].append(("FACT", claim, [evidence_id]))
                continue

            kind = statement.kind
            if kind == "ANALYSIS" and not valid_ids:
                adjustments.append(f"章节 {section_id} 删除无有效引用的判断")
                continue
            if kind == "RECOMMENDATION" and not valid_ids:
                kind = "HYPOTHESIS"
                adjustments.append(f"章节 {section_id} 将无直接依据的建议降级为待验证假设")
            if kind == "GAP":
                valid_ids = []
            if text:
                normalized[section_id].append((kind, text, valid_ids))

        if not normalized[section_id]:
            normalized[section_id].append(
                (
                    "GAP",
                    f"当前通过审核的证据不足以覆盖“{title}”，需补充一手或权威来源。",
                    [],
                )
            )
            adjustments.append(f"章节 {section_id} 自动补充研究缺口")

    all_kinds = [kind for statements in normalized.values() for kind, _, _ in statements]
    if "FACT" not in all_kinds:
        normalized["0"].insert(
            0,
            ("FACT", _clean_report_text(evidence_map[fallback_id].claim), [fallback_id]),
        )
        adjustments.append("自动补充一条逐字取自证据 Claim 的事实")
    if "ANALYSIS" not in all_kinds:
        normalized["13"].append(
            (
                "ANALYSIS",
                "基于当前已核验证据，只能形成有限判断；未被证据覆盖的部分不应外推。",
                [fallback_id],
            )
        )
        adjustments.append("自动补充证据边界判断")

    referenced_ids: list[str] = []
    lines = ["# 竞品研究报告", ""]
    for section_id, title in REPORT_SECTIONS.items():
        lines.extend([f"## {section_id}. {title}", ""])
        seen_lines: set[tuple[str, str, tuple[str, ...]]] = set()
        for kind, text, evidence_ids in normalized[section_id]:
            key = (kind, text, tuple(evidence_ids))
            if key in seen_lines:
                continue
            seen_lines.add(key)
            qualification_candidates: list[str] = []
            for evidence_id in evidence_ids:
                decision = decision_map.get(evidence_id)
                if decision is None or decision.status != "CAUTION":
                    continue
                cleaned_qualification = _clean_report_text(decision.required_qualification)
                if cleaned_qualification:
                    qualification_candidates.append(cleaned_qualification)
            qualifications = list(dict.fromkeys(qualification_candidates))
            qualification = f"（限定说明：{'；'.join(qualifications)}）" if qualifications else ""
            citations = " ".join(f"[{evidence_id}]" for evidence_id in evidence_ids)
            referenced_ids.extend(evidence_ids)
            suffix = f" {citations}" if citations else ""
            lines.extend([f"- {REPORT_LABELS[kind]}{text}{qualification}{suffix}", ""])

    unique_references = list(dict.fromkeys(referenced_ids))
    lines.extend(["## 15. Sources", ""])
    for evidence_id in unique_references:
        lines.extend([f"- [{evidence_id}] {evidence_map[evidence_id].url}", ""])
    return "\n".join(lines).rstrip() + "\n", list(dict.fromkeys(adjustments))


def _same_canonical_url(left: str, right: str) -> bool:
    try:
        return canonical_url(left) == canonical_url(right)
    except (TypeError, ValueError):
        return False


def validate_report(
    report: str,
    allowed_ids: set[str],
    evidence_urls: dict[str, str] | None = None,
) -> ReportValidation:
    errors: list[str] = []
    referenced = list(dict.fromkeys(_CITATION.findall(report)))
    unknown = sorted(set(referenced) - allowed_ids)
    if unknown:
        errors.append(f"引用了未通过门禁的证据：{', '.join(unknown)}")
    if allowed_ids and not referenced:
        errors.append("报告没有使用任何 [E##] 引用")
    if not report.lstrip().startswith("# 竞品研究报告"):
        errors.append("报告缺少规定的一级标题")
    required_sections = tuple(
        [f"## {section_id}. {title}" for section_id, title in REPORT_SECTIONS.items()]
        + ["## 15. Sources"]
    )
    for section in required_sections:
        if section not in report:
            errors.append(f"报告缺少章节：{section}")

    in_sources = False
    report_lines = report.splitlines()
    labels = ("【事实】", "【判断】", "【建议】", "【待验证假设】", "【研究缺口】")
    body_referenced: set[str] = set()
    source_referenced: set[str] = set()
    for line_number, raw_line in enumerate(report_lines, start=1):
        line = raw_line.strip()
        if line.startswith("## "):
            in_sources = line.startswith("## 15.")
            continue
        if not line or line.startswith("#") or re.fullmatch(r"\|?[\s:|-]+\|?", line):
            continue
        if in_sources:
            line_refs = set(_CITATION.findall(line))
            source_referenced.update(line_refs)
            if ("http://" in line or "https://" in line) and not line_refs:
                errors.append(f"第 {line_number} 行来源缺少 [E##]：{line[:80]}")
            if evidence_urls and line_refs:
                listed_urls = [match.rstrip(".,;，。") for match in re.findall(r"https?://[^\s)>]+", line)]
                for evidence_id in line_refs:
                    expected_url = evidence_urls.get(evidence_id)
                    if not expected_url:
                        continue
                    if not any(_same_canonical_url(listed_url, expected_url) for listed_url in listed_urls):
                        errors.append(f"第 {line_number} 行 {evidence_id} 的来源 URL 不匹配")
            continue
        body_referenced.update(_CITATION.findall(line))
        next_line = report_lines[line_number].strip() if line_number < len(report_lines) else ""
        is_table_header = line.startswith("|") and bool(re.fullmatch(r"\|?[\s:|-]+\|?", next_line))
        if not is_table_header and not any(label in line for label in labels):
            errors.append(f"第 {line_number} 行陈述未标注事实/判断/建议/缺口：{line[:80]}")
        if ("【事实】" in line or "【判断】" in line) and not _CITATION.search(line):
            errors.append(f"第 {line_number} 行事实/判断缺少证据引用：{line[:80]}")
        if "【建议】" in line and not _CITATION.search(line):
            errors.append(f"第 {line_number} 行建议没有证据；无依据时应标为【待验证假设】：{line[:80]}")
        if (
            _STRONG_FACT.search(line)
            and "【研究缺口】" not in line
            and "【待验证假设】" not in line
            and not _CITATION.search(line)
        ):
            errors.append(f"第 {line_number} 行含数字或日期但没有证据引用：{line[:80]}")
        bare = _BARE_EVIDENCE.findall(line)
        if bare:
            errors.append(f"第 {line_number} 行证据编号必须写成方括号格式：{', '.join(bare)}")

    missing_source_entries = sorted(body_referenced - source_referenced)
    if missing_source_entries:
        errors.append(f"Sources 缺少正文引用：{', '.join(missing_source_entries)}")
    unused_source_entries = sorted(source_referenced - body_referenced)
    if unused_source_entries:
        errors.append(f"Sources 列出了正文未使用的证据：{', '.join(unused_source_entries)}")

    if "【事实】" not in report:
        errors.append("报告没有明确标注【事实】")
    if "【判断】" not in report:
        errors.append("报告没有明确标注【判断】")
    return ReportValidation(
        valid=not errors,
        referenced_ids=referenced,
        errors=list(dict.fromkeys(errors))[:60],
    )


def render_evidence_markdown(ledger: EvidenceLedger) -> str:
    lines = ["# Evidence Ledger", ""]
    for item in ledger.evidence:
        lines.extend(
            [
                f"## {item.evidence_id}",
                "",
                f"- Claim: {item.claim}",
                f"- Claim Nature: {item.claim_nature}",
                f"- Source ID: {item.source_id}",
                f"- Source Tier: {item.source_tier}",
                f"- URL: {item.url}",
                f"- Published Date: {item.published_date}",
                f"- Evidence Type: {item.evidence_type}",
                f"- Confidence: {item.confidence}",
                f"- Evidence Text: {item.evidence_text}",
                f"- Reason: {item.reason}",
                "",
            ]
        )
    lines.extend(["# Missing Evidence", ""])
    lines.extend(f"{index}. {gap}" for index, gap in enumerate(ledger.missing_evidence, start=1))
    return "\n".join(lines).rstrip() + "\n"


def render_audit_markdown(audit: EffectiveAudit) -> str:
    lines = ["# Evidence Audit", ""]
    for item in audit.decisions:
        lines.extend(
            [
                f"## {item.evidence_id}",
                "",
                f"- Audit Status: {item.status}",
                f"- Approved Claim: {item.approved_claim}",
                f"- Source ID: {item.source_id}",
                f"- Source Tier: {item.source_tier}",
                f"- Confidence: {item.confidence}",
                f"- Required Qualification: {item.required_qualification or '无'}",
                f"- Audit Reason: {item.audit_reason}",
                "",
            ]
        )
    lines.extend(
        [
            "# Audit Summary",
            "",
            f"- Total Evidence: {audit.total_evidence}",
            f"- Approved: {len(audit.approved_ids)}",
            f"- Caution: {len(audit.caution_ids)}",
            f"- Rejected: {len(audit.rejected_ids)}",
            f"- Unreviewed: {len(audit.unreviewed_ids)}",
            f"- Approved Rate: {audit.approved_rate:.1%}",
            f"- Usable Rate: {audit.usable_rate:.1%}",
            f"- Overall Evidence Quality: {audit.overall_quality}",
            "",
            "## 主要风险",
            "",
        ]
    )
    lines.extend(f"{index}. {risk}" for index, risk in enumerate(audit.risks, start=1))
    return "\n".join(lines).rstrip() + "\n"
