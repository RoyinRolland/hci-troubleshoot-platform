from app.routes.admin import _derive_capability_gaps
from app.services.kbd_review_metadata import build_review_metadata, normalize_change_annotations


def _payload(signals):
    return {
        "title": "GPU 主机异常",
        "signals_json": {"schema_version": 2, "signals": signals},
        "payload_schema_version": 1,
    }


def test_delete_signal_uses_expert_reason_and_stable_signal_id():
    before = _payload(
        [
            {"id": "sig_001", "acquire": {"tool": "qkv_task"}},
            {"id": "sig_004", "acquire": {"tool": "qfk_system"}},
        ]
    )
    after = _payload([{"id": "sig_001", "acquire": {"tool": "qkv_task"}}])

    metadata = build_review_metadata(
        parent_payload=before,
        payload=after,
        annotations=normalize_change_annotations(
            [
                {
                    "signal_id": "sig_004",
                    "reason_code": "redundant_signal",
                    "note": "正常 GPU 主机示例不应在异常主机执行",
                }
            ]
        ),
        identity_status="unavailable",
        review_state="working",
    )

    change = metadata["change_summary"]["changes"][0]
    assert change["operation"] == "delete"
    assert change["path"].startswith("/signals_json/signals/sig_004")
    assert change["reason_code"] == "redundant_signal"
    assert change["reason_source"] == "expert"
    assert metadata["expert_gold"]["status"] == "not_eligible"


def test_unannotated_signal_add_gets_conservative_reason_code():
    metadata = build_review_metadata(
        parent_payload=_payload([]),
        payload=_payload([{"id": "sig_new", "acquire": {"tool": "qfk_log"}}]),
        identity_status="unavailable",
        review_state="working",
    )

    assert metadata["change_summary"]["reason_counts"] == {"missing_signal": 1}


def test_category_and_vision_expert_corrections_become_learning_labels():
    before = {
        "category_id": None,
        "ai_category_id": "vm-001",
        "images_json": [{"seq": 0, "desc": "AI 误读"}],
        "payload_schema_version": 1,
    }
    after = {
        "category_id": "storage-002",
        "ai_category_id": "vm-001",
        "images_json": [{"seq": 0, "desc": "专家确认的截图文字"}],
        "payload_schema_version": 1,
    }

    metadata = build_review_metadata(
        parent_payload=before,
        payload=after,
        identity_status="unavailable",
        review_state="working",
    )

    assert metadata["change_summary"]["reason_counts"] == {
        "screenshot_misread": 1,
        "wrong_category": 1,
    }
    labelled_paths = {
        item["path"]: item["reason_code"]
        for item in metadata["change_summary"]["changes"]
    }
    assert labelled_paths["/category_id"] == "wrong_category"
    assert labelled_paths["/images_json/0/desc"] == "screenshot_misread"


def test_rejects_uncontrolled_reason_code_and_unstable_target():
    try:
        normalize_change_annotations([{"signal_id": "sig_1", "reason_code": "因为我觉得不对"}])
    except ValueError as exc:
        assert "受控原因码" in str(exc)
    else:
        raise AssertionError("未拒绝自由原因码")

    try:
        normalize_change_annotations([{"reason_code": "redundant_signal"}])
    except ValueError as exc:
        assert "path 或 signal_id" in str(exc)
    else:
        raise AssertionError("未拒绝无目标标注")


def test_capability_gap_is_engineering_data_not_expert_todo():
    gaps = _derive_capability_gaps(
        {
            "schema_version": 2,
            "signals": [
                {"id": "sig_known", "acquire": {"tool": "qkv_task", "args": {}}},
                {"id": "sig_unknown", "acquire": {"tool": "filesystem.count_entries", "args": {}}},
            ],
        }
    )

    assert {
        (item["capability_id"], item["code"], item["signal_id"])
        for item in gaps
    } == {
        ("qkv_task", "CAPABILITY_RUNTIME_UNVERIFIED", "sig_known"),
        ("filesystem.count_entries", "CAPABILITY_UNDECLARED", "sig_unknown"),
    }


def test_rejected_candidate_dismissal_annotation_matches_payload_diff():
    """处置标注 path 必须命中 payload 根 diff 路径，reason_counts 落受控码而非推断码。

    回归：曾误用 '/rejected_candidates' 作标注 path，而 revision diff 路径以
    payload 为根（/signals_json/rejected_candidates/N/expert_dismissal），前缀
    匹配失败导致 reason_counts 落入 other_expert_correction，处置事实无法统计。
    """
    from app.routes.admin import KbdUpdateRequest, _rejected_disposal_annotation

    base_signals = {
        "schema_version": 2,
        "signals": [],
        "rejected_candidates": [
            {"candidate": {"id": "sig_002"}, "reason": "keyword 不可追溯", "reason_code": "run_failed"}
        ],
    }
    dismissed_signals = {
        "schema_version": 2,
        "signals": [],
        "rejected_candidates": [
            {
                "candidate": {"id": "sig_002"},
                "reason": "keyword 不可追溯",
                "reason_code": "run_failed",
                "expert_dismissal": {
                    "reviewed_by": "42",
                    "reviewed_at": "2026-10-10T08:00:00+00:00",
                    "note": "确认为纯文本知识",
                },
            }
        ],
    }
    before = {"title": "跨集群迁移", "signals_json": base_signals, "payload_schema_version": 1}
    after = {"title": "跨集群迁移", "signals_json": dismissed_signals, "payload_schema_version": 1}

    body = KbdUpdateRequest(
        dismiss_rejected_candidate_id="sig_002",
        dismiss_rejected_note="确认为纯文本知识",
        reviewer_id=42,
    )
    annotations = normalize_change_annotations(_rejected_disposal_annotation(body, []))
    assert annotations[0]["reason_code"] == "rejected_candidate_dismissed"

    metadata = build_review_metadata(
        parent_payload=before,
        payload=after,
        annotations=annotations,
        identity_status="unverified_body_reviewer_id",
        review_state="working",
    )
    counts = metadata["change_summary"]["reason_counts"]
    changes = metadata["change_summary"]["changes"]
    assert changes, "处置必须产生字段级差异"
    assert set(counts) == {"rejected_candidate_dismissed"}, counts
    assert all(change["reason_source"] == "expert" for change in changes)
