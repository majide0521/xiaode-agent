import unittest

from xiaode.models import AuditDecision, AuditReport, EvidenceItem, EvidenceLedger, SourceRecord
from xiaode.quality import (
    apply_quality_gate,
    requested_dimensions,
    validate_evidence_ledger,
    validate_report,
)


def source() -> SourceRecord:
    return SourceRecord(
        source_id="S01",
        title="Official pricing",
        url="https://example.com/pricing",
        domain="example.com",
        source_tier="Tier A",
        published_date="2026-09-01",
        quality_score=120,
        content="The Pro plan costs $20 per month and is available to individual subscribers.",
    )


def evidence(quote: str = "The Pro plan costs $20 per month") -> EvidenceItem:
    return EvidenceItem(
        evidence_id="E01",
        claim="The Pro plan costs $20 per month.",
        claim_nature="FACT",
        source_id="S01",
        source_name="Official pricing",
        source_tier="Tier A",
        url="https://example.com/pricing",
        published_date="2026-09-01",
        evidence_text=quote,
        evidence_type="价格",
        confidence="High",
        atomicity="PASS",
        entailment="PASS",
        reason="The quoted sentence directly states the price.",
    )


def audit(status: str = "APPROVED", qualification: str = "") -> AuditReport:
    return AuditReport(
        decisions=[
            AuditDecision(
                evidence_id="E01",
                status=status,
                approved_claim="The Pro plan costs $20 per month.",
                claim_nature="FACT",
                source_id="S01",
                source_tier="Tier A",
                confidence="High",
                required_qualification=qualification,
                audit_reason="The quote directly supports the claim.",
            )
        ],
        risks=[],
    )


class EvidenceGateTests(unittest.TestCase):
    def test_exact_quote_and_mapping_pass_precheck(self) -> None:
        ledger = EvidenceLedger(evidence=[evidence()], missing_evidence=[])
        self.assertEqual(validate_evidence_ledger(ledger, [source()]), {})

    def test_tampered_quote_is_rejected_by_code(self) -> None:
        ledger = EvidenceLedger(evidence=[evidence("The Enterprise plan costs $200")], missing_evidence=[])
        precheck = validate_evidence_ledger(ledger, [source()])
        result = apply_quality_gate(ledger, audit(), precheck, "商业模式和定价")
        self.assertEqual(result.audit.rejected_ids, ["E01"])
        self.assertEqual(result.audit.approved_ids, [])

    def test_caution_without_qualification_is_rejected(self) -> None:
        ledger = EvidenceLedger(evidence=[evidence()], missing_evidence=[])
        result = apply_quality_gate(ledger, audit("CAUTION"), {}, "商业模式和定价")
        self.assertEqual(result.audit.rejected_ids, ["E01"])

    def test_approval_and_usable_rates_are_distinct(self) -> None:
        ledger = EvidenceLedger(evidence=[evidence()], missing_evidence=[])
        result = apply_quality_gate(ledger, audit("CAUTION", "According to the official page"), {}, "商业模式")
        self.assertEqual(result.audit.approved_rate, 0.0)
        self.assertEqual(result.audit.usable_rate, 1.0)

    def test_china_dimension_is_focus_driven(self) -> None:
        self.assertNotIn("china", requested_dimensions("分析产品和商业模式"))
        self.assertIn("china", requested_dimensions("分析对百度和中国市场的威胁"))


class ReportValidationTests(unittest.TestCase):
    def _valid_report(self) -> str:
        sections = [
            "# 竞品研究报告",
            "## 0. Executive Summary",
            "## 1. 一句话判断",
            "## 2. 产品定位",
            "## 3. 核心用户与场景",
            "## 4. 核心产品能力",
            "## 5. 最近半年产品变化",
            "## 6. 用户与市场表现",
            "## 7. 商业模式",
            "## 8. 增长逻辑",
            "## 9. 核心竞争壁垒",
            "## 10. 主要问题与风险",
            "## 11. 对用户指定业务的威胁",
            "## 12. 值得借鉴的策略",
            "## 13. 最终判断",
            "【事实】Pro plan costs $20 per month. [E01]",
            "【判断】This supports a subscription positioning. [E01]",
            "## 14. 研究缺口",
            "【研究缺口】缺少留存率证据。",
            "## 15. Sources",
            "- [E01] https://example.com/pricing",
        ]
        return "\n\n".join(sections)

    def test_valid_report_passes(self) -> None:
        self.assertTrue(validate_report(self._valid_report(), {"E01"}).valid)

    def test_unknown_citation_fails(self) -> None:
        report = self._valid_report().replace("[E01]", "[E99]", 1)
        result = validate_report(report, {"E01"})
        self.assertFalse(result.valid)
        self.assertTrue(any("未通过门禁" in error for error in result.errors))

    def test_uncited_number_fails(self) -> None:
        report = self._valid_report().replace("$20 per month. [E01]", "$20 per month.", 1)
        self.assertFalse(validate_report(report, {"E01"}).valid)

    def test_wrong_source_url_fails(self) -> None:
        report = self._valid_report().replace("https://example.com/pricing", "https://example.com/about")
        result = validate_report(
            report,
            {"E01"},
            {"E01": "https://example.com/pricing"},
        )
        self.assertFalse(result.valid)
        self.assertTrue(any("URL 不匹配" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
