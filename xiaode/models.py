from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


APP_VERSION = "0.7.1"

SourceTier = Literal["Tier A", "Tier B", "Tier C", "Tier D"]
Confidence = Literal["High", "Medium", "Low"]
ClaimNature = Literal["FACT", "REPORTED_CLAIM"]
AuditStatus = Literal["APPROVED", "CAUTION", "REJECTED"]
ReportStatementKind = Literal["FACT", "ANALYSIS", "RECOMMENDATION", "HYPOTHESIS", "GAP"]
ReportSectionId = Literal[
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10",
    "11",
    "12",
    "13",
    "14",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResearchQuery(StrictModel):
    query: str = Field(min_length=3, max_length=240)
    dimension: Literal[
        "product",
        "users_market",
        "business",
        "growth",
        "competition",
        "technology",
        "china",
    ]
    preferred_source: Literal["official", "authority", "industry", "open"]


class SearchPlan(StrictModel):
    queries: list[ResearchQuery] = Field(min_length=3, max_length=10)


class SourceRecord(StrictModel):
    source_id: str = Field(pattern=r"^S\d{2,}$")
    title: str = Field(max_length=500)
    url: str = Field(max_length=2_000)
    domain: str
    source_tier: SourceTier
    published_date: str = Field(max_length=100)
    quality_score: int
    content: str = Field(min_length=1, max_length=12_000)


class EvidenceItem(StrictModel):
    evidence_id: str = Field(pattern=r"^E\d{2,}$")
    claim: str = Field(min_length=2, max_length=600)
    claim_nature: ClaimNature
    source_id: str = Field(pattern=r"^S\d{2,}$")
    source_name: str = Field(max_length=500)
    source_tier: SourceTier
    url: str = Field(max_length=2_000)
    published_date: str = Field(max_length=100)
    evidence_text: str = Field(min_length=2, max_length=2_000)
    evidence_type: Literal[
        "产品功能",
        "产品更新",
        "用户数据",
        "市场数据",
        "商业模式",
        "价格",
        "合作",
        "融资",
        "公司战略",
        "增长",
        "渠道",
        "技术能力",
        "组织",
        "竞争",
        "其他",
    ]
    confidence: Confidence
    atomicity: Literal["PASS"]
    entailment: Literal["PASS"]
    reason: str = Field(min_length=2, max_length=800)


class EvidenceLedger(StrictModel):
    evidence: list[EvidenceItem] = Field(max_length=24)
    missing_evidence: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_ids(self) -> "EvidenceLedger":
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence_id must be unique")
        return self


class AuditDecision(StrictModel):
    evidence_id: str = Field(pattern=r"^E\d{2,}$")
    status: AuditStatus
    approved_claim: str = Field(max_length=600)
    claim_nature: ClaimNature
    source_id: str = Field(pattern=r"^S\d{2,}$")
    source_tier: SourceTier
    confidence: Confidence
    required_qualification: str = Field(max_length=500)
    audit_reason: str = Field(min_length=2, max_length=800)


class AuditReport(StrictModel):
    decisions: list[AuditDecision] = Field(max_length=24)
    risks: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def unique_ids(self) -> "AuditReport":
        ids = [item.evidence_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("audited evidence_id must be unique")
        return self


class EffectiveAudit(StrictModel):
    decisions: list[AuditDecision]
    risks: list[str]
    approved_ids: list[str]
    caution_ids: list[str]
    rejected_ids: list[str]
    unreviewed_ids: list[str]
    total_evidence: int
    approved_rate: float
    usable_rate: float
    overall_quality: Literal["HIGH", "MEDIUM", "LOW"]


class CoverageItem(StrictModel):
    dimension: str
    label: str
    required: int
    evidence_ids: list[str]
    covered: bool


class QualityResult(StrictModel):
    audit: EffectiveAudit
    precheck_errors: dict[str, list[str]]
    evidence_coverage: list[CoverageItem]
    writer_packet: str


class ReportStatement(StrictModel):
    kind: ReportStatementKind
    text: str = Field(min_length=2, max_length=1_200)
    evidence_ids: list[str] = Field(max_length=8)


class ReportSection(StrictModel):
    section_id: ReportSectionId
    statements: list[ReportStatement] = Field(min_length=1, max_length=8)


class ReportDraft(StrictModel):
    sections: list[ReportSection] = Field(min_length=1, max_length=15)


class ReportValidation(StrictModel):
    valid: bool
    referenced_ids: list[str]
    errors: list[str]
