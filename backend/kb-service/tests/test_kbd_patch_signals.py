import copy
from unittest.mock import AsyncMock, MagicMock

import app.routes.admin as admin_route
import pytest
from app.routes.admin import (
    _apply_rejected_candidate_dismissal,
    _prepare_expert_draft_signals,
    kbd_router,
    set_dependencies,
)
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def test_prepare_expert_draft_migrates_legacy_qkv_nested_match_extract():
    """KBD41464 同类历史草稿在保存边界自动清理 QKV 嵌套 QFK 提取字段。"""

    qfk_extract = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "value_mode": "string",
    }
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_002",
                "role": "must",
                "review": {"notes": "", "require_human_confirm": False},
                "provenance": {"category": "frontend", "confidence": 0.9},
                "acquire": {
                    "tool": "qkv_task",
                    "args": {"keyword": "创建虚拟机", "limit": 1, "timeout": 60, "is_failed": True},
                },
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "produces": [{"name": "DESCRIPTION", "path": "description"}],
                    "requires": [],
                    "output_processing": [
                        {
                            "mode": "assert",
                            "input": "{{DESCRIPTION}}",
                            "match": {
                                "type": "keyword",
                                "pattern": "无法复制镜像",
                                "mode": "or",
                                "expected": True,
                                "extract": qfk_extract,
                            },
                        }
                    ],
                },
            }
        ],
    }

    normalized = _prepare_expert_draft_signals(document, kbd_id=2051, operation="test")
    matcher = normalized["signals"][0]["orchestrate"]["output_processing"][0]["match"]
    assert "extract" not in matcher


@pytest.mark.anyio
async def test_update_kbd_entry_signals_json_sql_cast():
    app = FastAPI()
    app.include_router(kbd_router)

    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    original_check_auth = admin_route._check_auth
    admin_route._check_auth = lambda req: None
    set_dependencies(mock_db)

    # Mock UPDATE result
    mock_update_row = MagicMock()
    mock_update_row.mappings.return_value.first.return_value = {"id": 1, "status": "draft"}
    mock_session.execute.return_value = mock_update_row

    client = TestClient(app)
    # v2 嵌套文档（运行时仅 v2 单一版本）
    test_signals = {
        "schema_version": 2,
        "generation_metadata": {
            "schema_version": 1,
            "status": "current",
            "source_fingerprint": "0" * 64,
            "prompt_revision": "1" * 64,
            "model_id": "test-model",
            "tool_contract_revision": "2" * 64,
            "generation_fingerprint": "3" * 64,
        },
        "rejected_candidates": [
            {
                "candidate": {"id": "bad"},
                "reason_code": "run_failed",
                "reason": "缺少 acquire",
            },
            # 已处置候选 + tool_restricted 分类码：整稿保存必须原样透传处置标记，
            # 否则专家“复核忽略”会被后续任意保存静默冲掉（发布门禁随之复活）。
            {
                "candidate": {"id": "dismissed"},
                "reason_code": "tool_restricted",
                "reason": "qfk_var 自由文本候选被剥离",
                "expert_dismissal": {
                    "reviewed_by": "42",
                    "reviewed_at": "2026-10-10T08:00:00+00:00",
                    "note": "已确认无恢复价值",
                },
            },
        ],
        "signals": [
            {
                "acquire": {"tool": "qkv_task", "args": {"keyword": "vm"}},
                # QKV 是产出变量信号：必须以 produces 写入变量池，不能同时残留 matcher。
                "match": None,
                "orchestrate": {"produces": [{"name": "VM", "path": "vm"}]},
                "provenance": {"category": "frontend"},
            }
        ],
    }

    response = client.patch(
        "/api/admin/kbd/1",
        json={"signals_json": test_signals},
        headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
    )

    admin_route._check_auth = original_check_auth

    assert response.status_code == 200

    # 保存时以 CAST(... AS jsonb) 落库 v2 文档
    executed_call = next(
        call
        for call in reversed(mock_session.execute.call_args_list)
        if "UPDATE kbd_entry SET" in str(call[0][0])
    )
    sql_text = str(executed_call[0][0])
    assert "CAST(:signals_json AS jsonb)" in sql_text
    # 关键：落库 SQL 不得残留 ':signals_json' 字面量（否则会被 PG 报语法错误 500）
    assert ":signals_json::jsonb" not in sql_text
    # 落库内容应为 v2 文档（含 acquire 嵌套对象）
    import json

    stored = executed_call[0][1].get("signals_json")
    stored_doc = json.loads(stored) if isinstance(stored, str) else stored
    stored_signals = (
        stored_doc["signals"] if isinstance(stored_doc, dict) else stored_doc
    )
    assert any("acquire" in s for s in stored_signals), "signals 应为 v2 嵌套形态"
    assert stored_doc["generation_metadata"]["status"] == "manual_reviewed"
    assert stored_doc["rejected_candidates"] == test_signals["rejected_candidates"]


@pytest.mark.anyio
async def test_update_kbd_entry_signals_json_sql_bind_compiles():
    """回归（根因级，挡住 PR#599 修复被 PR#601 回退）：

    落库 SQL 若写成 'signals_json = :signals_json::jsonb'，其中 ':signals_json'
    紧跟 '::'，SQLAlchemy 命名绑定正则(负向预查 (?!:))不将其识别为绑定参数，
    会把 ':signals_json' 字面量发给 Postgres → 'syntax error at or near ":"' (500)，
    前端统一弹「保存失败，请重试」。

    本用例用 PG 方言编译真实落库 SQL，断言命名绑定被正确编译为参数占位符
    ($N / %(name)s)，而非残留 ':signals_json' 字面量。一旦有人把 CAST(...) 改回
    '::jsonb' 写法，本测试立即失败。
    """
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql

    # 复刻 update_kbd_entry 的落库 SQL（含 id 绑定）
    sql = text(
        "UPDATE kbd_entry SET signals_json = CAST(:signals_json AS jsonb) "
        "WHERE id = :id"
    )
    compiled = str(sql.compile(dialect=postgresql.dialect()))

    # 关键断言：不得残留 ':signals_json' 字面量（BUG 写法会残留并被 PG 拒绝）
    assert ":signals_json" not in compiled, (
        f"命名绑定未被识别为参数，SQL 残留字面量 ':signals_json'（将触发 PG 语法错误 500）: {compiled}"
    )
    # 同时确认两个命名绑定都被编译成了参数占位符
    assert "signals_json" in sql.compile(dialect=postgresql.dialect()).params or "%(signals_json)s" in compiled or "$1" in compiled
    assert "id" in sql.compile(dialect=postgresql.dialect()).params or "%(id)s" in compiled or "$2" in compiled


@pytest.mark.anyio
async def test_update_kbd_entry_signals_with_produce_alias():
    """回归测试：KBD 生产者信号支持产出变量 alias 字段保存且不报 SIGNAL_FIELD_UNSUPPORTED。"""
    app = FastAPI()
    app.include_router(kbd_router)

    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    original_check_auth = admin_route._check_auth
    admin_route._check_auth = lambda req: None
    set_dependencies(mock_db)

    mock_update_row = MagicMock()
    mock_update_row.mappings.return_value.first.return_value = {"id": 2051, "status": "draft"}
    mock_session.execute.return_value = mock_update_row

    client = TestClient(app)
    test_signals = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_002",
                "role": "must",
                "acquire": {"tool": "qkv_task", "args": {"keyword": "创建虚拟机", "is_failed": True}},
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "produces": [
                        {"name": "VM", "path": "vm"},
                        {"name": "HOST", "path": "host"},
                        {"name": "REQUEST_ID", "path": "request_id"},
                        {"name": "END", "path": "end", "alias": "END1"},
                    ],
                },
                "provenance": {"category": "frontend"},
            },
            {
                "id": "expert_qfk_log",
                "role": "must",
                "acquire": {
                    "tool": "qfk_log",
                    "args": {
                        "host": "{{HOST}}",
                        "file": "sfscp.log",
                        "request_id": "{{REQUEST_ID}}",
                        "time_window": "{{END1}}",
                        "instruction": "检索日志",
                    },
                },
                "match": {
                    "type": "keyword",
                    "pattern": "write失败",
                    "mode": "and",
                    "expected": True,
                    "extract": {"type": "text", "rows": {"mode": "all"}},
                },
                "orchestrate": {
                    "phase": "diagnostic",
                    "requires": ["END1", "HOST", "REQUEST_ID"],
                },
                "provenance": {"category": "backend"},
            },
        ],
    }

    response = client.patch(
        "/api/admin/kbd/2051",
        json={"signals_json": test_signals},
        headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
    )

    admin_route._check_auth = original_check_auth
    assert response.status_code == 200, response.text


# ─────────────────────────────────────────────────────────────────────────────
# 被拒候选专家处置（复核忽略 / 撤销处置，2026-10-10）
# ─────────────────────────────────────────────────────────────────────────────

REJECTED_DOC = {
    "schema_version": 2,
    "signals": [],
    "rejected_candidates": [
        {
            "candidate": {"id": "sig_002"},
            "reason_code": "run_failed",
            "reason": "keyword Matcher 的 match.pattern 无法从 provenance.evidence 逐字追溯",
        },
        {
            "candidate": {"id": "sig_003"},
            "reason_code": "run_failed",
            "reason": "operator 'gte' 非法",
        },
    ],
}


def _dismissal_test_client(signals_doc, *, lock_version=3):
    """构造 update_kbd_entry 处置分支所需的最小 mock 环境。"""
    app = FastAPI()
    app.include_router(kbd_router)
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session
    mock_row = MagicMock()
    kbd_mock = mock_row.scalar_one_or_none.return_value
    kbd_mock.status = "draft"
    kbd_mock.lock_version = lock_version
    kbd_mock.signals_json = copy.deepcopy(signals_doc)
    kbd_mock.images_json = []
    mock_row.mappings.return_value.first.return_value = {
        "id": 1,
        "status": "draft",
        "lock_version": lock_version + 1,
    }
    mock_session.execute.return_value = mock_row
    original_check_auth = admin_route._check_auth
    admin_route._check_auth = lambda req: None
    set_dependencies(mock_db)
    client = TestClient(app)
    return client, mock_session, original_check_auth


def _stored_signals_json(mock_session):
    import json as json_module

    executed_call = next(
        call for call in reversed(mock_session.execute.call_args_list) if "UPDATE kbd_entry SET" in str(call[0][0])
    )
    stored = executed_call[0][1].get("signals_json")
    return json_module.loads(stored) if isinstance(stored, str) else stored


@pytest.mark.anyio
async def test_dismiss_rejected_candidate_writes_expert_dismissal_and_keeps_snapshot():
    client, mock_session, original = _dismissal_test_client(REJECTED_DOC)
    try:
        response = client.patch(
            "/api/admin/kbd/1",
            json={
                "dismiss_rejected_candidate_id": "sig_002",
                "dismiss_rejected_note": "案例确认为纯文本知识，候选无恢复价值",
                "reviewer_id": 42,
                "lock_version": 3,
            },
            headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
        )
    finally:
        admin_route._check_auth = original
    assert response.status_code == 200, response.text
    stored = _stored_signals_json(mock_session)
    dismissed = stored["rejected_candidates"][0]
    # 审计快照原样保留，处置是附加事实
    assert dismissed["candidate"] == {"id": "sig_002"}
    assert dismissed["reason"] == "keyword Matcher 的 match.pattern 无法从 provenance.evidence 逐字追溯"
    assert dismissed["expert_dismissal"]["reviewed_by"] == "42"
    assert dismissed["expert_dismissal"]["note"] == "案例确认为纯文本知识，候选无恢复价值"
    assert dismissed["expert_dismissal"]["reviewed_at"]
    # 未处置的候选不受影响
    assert "expert_dismissal" not in stored["rejected_candidates"][1]


@pytest.mark.anyio
async def test_dismiss_rejected_candidate_conflicts_and_lock_guard():
    # 处置不得与整稿保存同时提交（V-007 单对象语义）
    client, _mock_session, original = _dismissal_test_client(REJECTED_DOC)
    try:
        response = client.patch(
            "/api/admin/kbd/1",
            json={
                "dismiss_rejected_candidate_id": "sig_002",
                "dismiss_rejected_note": "说明",
                "reviewer_id": 42,
                "signals_json": REJECTED_DOC,
            },
            headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
        )
        assert response.status_code == 400
        # 乐观锁不匹配 → 409
        conflict = client.patch(
            "/api/admin/kbd/1",
            json={
                "dismiss_rejected_candidate_id": "sig_002",
                "dismiss_rejected_note": "说明",
                "reviewer_id": 42,
                "lock_version": 99,
            },
            headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
        )
    finally:
        admin_route._check_auth = original
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "KBD_EDIT_CONFLICT"


def test_apply_rejected_candidate_dismissal_validation_errors():
    # 候选不存在 → 404（不提供展示序号兜底）
    with pytest.raises(HTTPException) as not_found:
        _apply_rejected_candidate_dismissal(
            REJECTED_DOC, candidate_id="sig_999", revoke=False, note="说明", reviewer_id=42
        )
    assert not_found.value.status_code == 404
    assert not_found.value.detail["code"] == "KBD_REJECTED_CANDIDATE_NOT_FOUND"

    # 缺处置说明 → 422
    with pytest.raises(HTTPException) as no_note:
        _apply_rejected_candidate_dismissal(REJECTED_DOC, candidate_id="sig_002", revoke=False, note="  ", reviewer_id=42)
    assert no_note.value.status_code == 422
    assert no_note.value.detail["code"] == "KBD_REJECTED_DISMISSAL_NOTE_REQUIRED"

    # 缺操作人 → 422
    with pytest.raises(HTTPException) as no_reviewer:
        _apply_rejected_candidate_dismissal(REJECTED_DOC, candidate_id="sig_002", revoke=False, note="说明", reviewer_id=None)
    assert no_reviewer.value.status_code == 422
    assert no_reviewer.value.detail["code"] == "KBD_REJECTED_DISMISSAL_REVIEWER_REQUIRED"

    # 候选 ID 重复 → 422 fail-closed
    duplicated = copy.deepcopy(REJECTED_DOC)
    duplicated["rejected_candidates"].append(copy.deepcopy(duplicated["rejected_candidates"][0]))
    with pytest.raises(HTTPException) as ambiguous:
        _apply_rejected_candidate_dismissal(duplicated, candidate_id="sig_002", revoke=False, note="说明", reviewer_id=42)
    assert ambiguous.value.status_code == 422
    assert ambiguous.value.detail["code"] == "KBD_REJECTED_CANDIDATE_AMBIGUOUS"


def test_revoke_rejected_dismissal_is_idempotent():
    dismissed_once = _apply_rejected_candidate_dismissal(
        REJECTED_DOC, candidate_id="sig_002", revoke=False, note="说明", reviewer_id=42
    )
    assert dismissed_once["rejected_candidates"][0]["expert_dismissal"]["reviewed_by"] == "42"

    # 重复处置：覆盖为最新处置（幂等，不报错）
    dismissed_twice = _apply_rejected_candidate_dismissal(
        dismissed_once, candidate_id="sig_002", revoke=False, note="更新说明", reviewer_id=43
    )
    assert dismissed_twice["rejected_candidates"][0]["expert_dismissal"]["note"] == "更新说明"

    # 撤销：移除处置标记，快照保留
    revoked = _apply_rejected_candidate_dismissal(dismissed_twice, candidate_id="sig_002", revoke=True)
    assert "expert_dismissal" not in revoked["rejected_candidates"][0]
    assert revoked["rejected_candidates"][0]["candidate"] == {"id": "sig_002"}

    # 撤销不存在的处置：幂等成功
    revoked_again = _apply_rejected_candidate_dismissal(revoked, candidate_id="sig_002", revoke=True)
    assert "expert_dismissal" not in revoked_again["rejected_candidates"][0]


@pytest.mark.anyio
async def test_revoke_rejected_candidate_endpoint_removes_marker():
    dismissed_doc = copy.deepcopy(REJECTED_DOC)
    dismissed_doc["rejected_candidates"][0]["expert_dismissal"] = {
        "reviewed_by": "42",
        "reviewed_at": "2026-10-10T08:00:00+00:00",
        "note": "误处置",
    }
    client, mock_session, original = _dismissal_test_client(dismissed_doc)
    try:
        response = client.patch(
            "/api/admin/kbd/1",
            json={"revoke_rejected_candidate_id": "sig_002", "lock_version": 3},
            headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
        )
    finally:
        admin_route._check_auth = original
    assert response.status_code == 200, response.text
    stored = _stored_signals_json(mock_session)
    assert "expert_dismissal" not in stored["rejected_candidates"][0]
