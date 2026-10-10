"""知识可推荐与现场可验证的能力边界，不为纯文本案例伪造 Signal。"""

from typing import Any


def _is_dismissed_rejected_candidate(item: Any) -> bool:
    """单条被拒候选是否已经专家显式处置（复核忽略）。

    fail-closed：非 dict、缺 expert_dismissal、处置记录字段不合规
    （reviewed_by/reviewed_at/note 任一为空，与 Schema required 契约一致）
    一律视为未处置。处置记录由 kb-service 的 PATCH 处置分支写入，
    形态受 signal.v2.schema.json 约束。
    """
    if not isinstance(item, dict):
        return False
    dismissal = item.get("expert_dismissal")
    if not isinstance(dismissal, dict):
        return False
    return all(
        str(dismissal.get(field) or "").strip() for field in ("reviewed_by", "reviewed_at", "note")
    )


def all_rejected_candidates_dismissed(raw: dict[str, Any]) -> bool:
    """被拒候选列表是否已全部经专家显式处置。

    空列表/缺失视为已清空（True）；非列表的畸形值 fail-closed（False）。
    """
    items = raw.get("rejected_candidates")
    if not items:
        return True
    if not isinstance(items, list):
        return False
    return all(_is_dismissed_rejected_candidate(item) for item in items)


def is_reference_only(raw: Any) -> bool:
    """仅识别干净的空信号文档；抽取异常和孤儿画像不能自动降级。

    被拒候选不阻断“专家显式降级”：全部候选带合规 expert_dismissal
    处置记录时放行（《关键信号抽取与建模设计》§复核语义——“未处理的
    拒绝候选仍须明确复核”，处置即复核留痕）；管道自动忽略仍被禁止，
    任何未处置/畸形项都维持拦截。
    """
    if raw is None or raw == [] or raw == {}:
        return True
    if not isinstance(raw, dict) or raw.get("signals") != [] or raw.get("semantic_entry_profile") is not None:
        return False
    metadata = raw.get("generation_metadata") or {}
    return (
        isinstance(metadata, dict)
        and metadata.get("status") not in {"stale", "failed", "error", "running", "pending"}
        and all_rejected_candidates_dismissed(raw)
    )
