"""纯文本知识与自动诊断的边界，以及标题召回的正反例。"""

from unittest.mock import AsyncMock

import pytest
from jsonschema import ValidationError
from shared.schemas.kbd_recommendation import recommend_cases
from shared.schemas.semantic_entry import capability_of
from shared.schemas.semantic_routing import resolve_candidates
from shared.schemas.signal_schema import (
    validate_draft_signals_json,
    validate_kbd_publishable_signals_json,
    validate_signals_json,
)


def dismissed_candidate(identifier="sig_002"):
    """构造一条已经专家显式处置（复核忽略）的被拒候选。"""
    return {
        "candidate": {"id": identifier},
        "reason": "keyword Matcher 的 match.pattern 无法从 provenance.evidence 逐字追溯",
        "reason_code": "run_failed",
        "expert_dismissal": {
            "reviewed_by": "42",
            "reviewed_at": "2026-10-10T08:00:00+00:00",
            "note": "已确认该案例为纯文本知识，候选无恢复价值",
        },
    }


def reference(identifier="1", title="安装 Windows Server 2016 提示缺少介质驱动程序", **fields):
    return {
        "id": identifier,
        "support_id": "15936",
        "title": title,
        "problem_description": "使用 ISO 安装 Windows Server 2016，提示缺少介质驱动程序",
        "root_cause": "历史案例根因",
        "solution": "历史案例处理方法",
        "signals_json": {"schema_version": 2, "signals": []},
        "resource_revision": {"revision": 3},
        "executable": False,
        **fields,
    }


def test_empty_document_is_publishable_reference_and_invalid_signal_still_fails():
    document = reference()["signals_json"]
    validate_kbd_publishable_signals_json(document)
    assert capability_of(document) == "reference_only"
    with pytest.raises(ValidationError):
        validate_kbd_publishable_signals_json({**document, "signals": [{"id": "bad"}]})
    with pytest.raises(ValidationError):
        validate_kbd_publishable_signals_json({**document, "semantic_entry_profile": {}})
    assert capability_of({**document, "rejected_candidates": [{"reason": "failed"}]}) is None
    assert capability_of({**document, "generation_metadata": {"status": "stale"}}) is None


def test_dismissed_rejected_candidates_allow_explicit_reference_downgrade():
    """全部被拒候选经专家显式处置后，空信号文档可降级为仅案例推荐。"""
    document = reference()["signals_json"]
    resolved = {**document, "rejected_candidates": [dismissed_candidate()]}
    # 处置记录本身必须是合法 Schema 形态（工作稿/发布/抽取落库共用校验）
    validate_signals_json(resolved)
    validate_draft_signals_json(resolved)
    validate_kbd_publishable_signals_json(resolved)
    assert capability_of(resolved) == "reference_only"

    # 部分处置：仍视为“存在未处理的拒绝候选”，继续拦截
    partial = {
        **document,
        "rejected_candidates": [
            dismissed_candidate(),
            {"candidate": {"id": "sig_003"}, "reason": "operator 'gte' 非法", "reason_code": "run_failed"},
        ],
    }
    assert capability_of(partial) is None
    with pytest.raises(ValidationError):
        validate_kbd_publishable_signals_json(partial)

    # 畸形处置记录：fail-closed，一律按未处置处理
    for bad_dismissal in (
        None,
        "dismissed",
        {"reviewed_by": "42", "reviewed_at": "", "note": "有说明"},
        {"reviewed_by": "42", "reviewed_at": "2026-10-10T08:00:00+00:00", "note": "   "},
        {"reviewed_at": "2026-10-10T08:00:00+00:00", "note": "有说明"},
    ):
        malformed = {**dismissed_candidate(), "expert_dismissal": bad_dismissal}
        assert capability_of({**document, "rejected_candidates": [malformed]}) is None
    # 候选项本身畸形（非 dict）同样 fail-closed
    assert capability_of({**document, "rejected_candidates": ["not-a-dict"]}) is None
    # rejected_candidates 畸形为非列表时 fail-closed
    assert capability_of({**document, "rejected_candidates": {"candidate": {}}}) is None


def test_tool_restricted_reason_code_is_schema_valid():
    """qfk_var 剥离使用的 tool_restricted 分类码必须能通过文档校验（回归既有 enum 缺失 bug）。"""
    document = {
        "schema_version": 2,
        "signals": [],
        "rejected_candidates": [
            {
                "candidate": {"id": "sig_001", "acquire": {"tool": "qfk_var"}},
                "reason": "qfk_var 自由文本候选被剥离，禁止 LLM 自由发明现场命令",
                "reason_code": "tool_restricted",
            }
        ],
    }
    validate_signals_json(document)
    validate_draft_signals_json(document)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_applicable", "no_match", "matched_inconclusive", "source_unavailable"])
async def test_title_only_case_is_recommended_without_becoming_executable(status):
    result = await resolve_candidates(
        entries=[reference()],
        context="安装 Windows Server 2016 提示缺少介质驱动程序",
        strong_status=status,
        include_reference_cases=True,
    )
    assert result["decision"] == "case_recommendations"
    candidate = result["candidates"][0]
    assert candidate["kbd_id"] == "1"
    assert candidate["verified"] is False
    assert candidate["resource_revision"]["revision"] == 3
    assert candidate["recommendation_solution"] == "历史案例处理方法"
    assert result["strong_producer_status"] == status


@pytest.mark.asyncio
async def test_rejected_candidate_cannot_return_through_title_matching():
    result = await resolve_candidates(
        entries=[reference()],
        context="缺少介质驱动程序",
        strong_status="no_match",
        include_reference_cases=True,
        excluded_kbd_ids=["1"],
    )
    assert result["candidates"] == []
    assert result["decision"] != "case_recommendations"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["虚拟机问题", "没有出现缺少介质驱动程序", "以前缺少介质驱动程序；现在登录缓慢", "备份存储池满了"]
)
async def test_generic_negative_historical_and_unrelated_queries_do_not_force_a_match(query):
    assert await recommend_cases(entries=[reference()], context=query) is None


@pytest.mark.asyncio
async def test_root_cause_does_not_become_retrieval_input():
    entry = reference(root_cause="备份池磁盘空间不足", solution="清理备份池空间")
    assert await recommend_cases(entries=[entry], context="备份池磁盘空间不足") is None


@pytest.mark.asyncio
async def test_same_symptom_different_causes_remain_multiple_references():
    entries = [reference("1"), reference("2", root_cause="另一种原因")]
    result = await recommend_cases(entries=entries, context="缺少介质驱动程序")
    assert len(result["candidates"]) == 2
    assert result["ambiguous"] is True
    assert result["next_action"]["type"] == "compare_cases"
    assert all(candidate["verified"] is False for candidate in result["candidates"])


@pytest.mark.asyncio
async def test_vector_synonym_recall_and_outage_lexical_fallback():
    embed = AsyncMock(side_effect=[[[1.0, 0.0]], [[1.0, 0.0]]])
    result = await recommend_cases(entries=[reference()], context="系统部署过程报告找不到安装设备驱动", embed=embed)
    assert result["candidates"][0]["score_parts"]["vector"] == 1.0
    failed = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    result = await recommend_cases(entries=[reference()], context="缺少介质驱动程序", embed=failed)
    assert result["degraded"] is True
    assert result["candidates"][0]["score_parts"]["vector"] == 0


@pytest.mark.asyncio
async def test_explicit_profile_exclusions_and_version_scope_are_retained():
    entry = reference()
    entry["signals_json"]["semantic_entry_profile"] = {
        "diagnosis_capability": "guidance_only",
        "canonical_symptoms": ["缺少介质驱动程序"],
        "positive_anchors": ["缺少介质驱动程序"],
        "exclusion_anchors": ["磁盘故障"],
        "applicability": {"product_version": ["6.*"]},
    }
    assert (
        await recommend_cases(entries=[entry], context={"description": "缺少介质驱动程序", "product_version": "7.0"})
        is None
    )
    assert await recommend_cases(entries=[entry], context="缺少介质驱动程序，同时磁盘故障") is None
    result = await recommend_cases(entries=[entry], context="缺少介质驱动程序")
    assert result["candidates"][0]["scope_warning"] == "missing_scope:product_version"


@pytest.mark.asyncio
async def test_recommendation_only_never_restarts_an_executable_semantic_candidate():
    entry = reference(executable=True)
    entry["signals_json"] = {
        "signals": [{"acquire": {"tool": "qkv_case_context"}}],
        "semantic_entry_profile": {
            "diagnosis_capability": "executable",
            "canonical_symptoms": ["缺少介质驱动程序"],
            "positive_anchors": ["缺少介质驱动程序"],
        },
    }
    result = await resolve_candidates(
        entries=[entry],
        context="缺少介质驱动程序",
        strong_status="not_applicable",
        include_reference_cases=True,
        recommendation_only=True,
    )
    assert result["decision"] == "case_recommendations"
    assert result["candidates"][0]["verified"] is False
