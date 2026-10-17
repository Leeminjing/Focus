"""本文件提供 CausalInputReadView 的输入时点因果变化投影。

输入为前后不可变快照及用户引用；输出为有界变化、准确事实修订和原快照 hash。
具体工作流为受理时按实体身份比较列表、冻结引用正文，后续消费不读历史整包。
用户输入与可见预览独立呈现；首条基线与省略内容明确标记，原快照保持不变。
示例：CausalInputReadView.build(snapshot, previous=old)。
"""

from copy import deepcopy

from sqlalchemy import or_, select

from backend.app.desktop.agent_loop.fact_models import LoopFactRevision
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.agent_loop.patrol_read_view import PatrolReadView


_IDENTITIES = ("fact_id", "round_id", "run_id", "directive_id", "worker_request_id",
               "dispatch_id", "portfolio_revision_id", "adoption_id", "context_id",
               "intent_id", "failure_id", "request_id", "source_key", "check_id")
_MISSING = object()
_CHANGE_BYTES = 16384


def _changes(before, after, path=()):
    if before is after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            yield from _changes(before.get(key, _MISSING), after.get(key, _MISSING), (*path, key))
        return
    if isinstance(before, list) and isinstance(after, list) and (before or after):
        rows = before + after
        for key in _IDENTITIES:
            if all(isinstance(row, dict) and isinstance(row.get(key), (str, int)) for row in rows):
                old, new = {row[key]: row for row in before}, {row[key]: row for row in after}
                if len(old) == len(before) and len(new) == len(after):
                    if list(old) != list(new):
                        yield {"path": list(path), "identity_order": key,
                               "before": list(old), "after": list(new)}
                    for identity in sorted(old.keys() | new.keys(), key=str):
                        yield from _changes(old.get(identity, _MISSING), new.get(identity, _MISSING),
                                            (*path, {key: identity}))
                    return
        if len(before) == len(after):
            for index, (old, new) in enumerate(zip(before, after)):
                yield from _changes(old, new, (*path, index))
            return
    if type(before) is type(after) and before == after:
        return
    yield {"path": list(path),
           **({"before": before} if before is not _MISSING else {}),
           **({"after": after} if after is not _MISSING else {})}


class CausalInputReadView:
    @staticmethod
    def build(snapshot, *, previous=None, previous_source=None, referenced_facts=()):
        changes, used, omitted = [], 2, 0
        if previous is not None:
            def state(value):
                return {key: ({name: item for name, item in value[key].items() if name != "visible_context"}
                              if key == "intervention" and isinstance(value[key], dict) else value[key])
                        for key in value if key != "user_intents"}

            old, new = state(previous), state(snapshot)
            for change in _changes(old, new):
                size = len(PatrolReadView._encoded(change)) + bool(changes)
                if used + size <= _CHANGE_BYTES:
                    changes.append(change)
                    used += size
                else:
                    omitted += 1
        return deepcopy({
            "snapshot_hash": canonical_hash(snapshot),
            "comparison": "previous_input" if previous is not None else "baseline_only",
            "previous": {key: previous_source[key] for key in
                         ("intent_id", "observation_id", "generation", "snapshot_hash") if key in previous_source}
                        if previous_source else None,
            "changes": changes,
            "omitted_changes": omitted,
            "complete": previous is not None and omitted == 0,
            "separately_presented": ["user_intents", "intervention.visible_context"],
            "visible_context": (snapshot.get("intervention") or {}).get("visible_context"),
            "referenced_facts": list(referenced_facts),
        })

    @classmethod
    async def capture(cls, session, loop_id, snapshot, request, *, previous_source=None):
        from backend.app.desktop.agent_loop.causal_snapshot import read_causal_snapshot

        previous = await read_causal_snapshot(session, loop_id, previous_source) if (
            previous_source and previous_source.get("snapshot_hash")) else None
        visible = (snapshot.get("intervention") or {}).get("visible_context") or {}
        references = {(item["fact_id"], item["revision"]) for item in visible.get("fact_refs", ())}
        references.update((item["fact_id"], item["revision"]) for item in request.get("file_refs", ()))
        facts = []
        if references:
            revisions = await session.scalars(select(LoopFactRevision).where(
                or_(*((LoopFactRevision.fact_id == fact_id) & (LoopFactRevision.revision == revision)
                       for fact_id, revision in references))))
            for row in revisions:
                fact = {key: getattr(row, key) for key in ("fact_id", "revision", "state", "presentation",
                                                          "evidence", "observer", "verifier")}
                facts.append({**fact, "content_hash": canonical_hash(fact)})
            if {(item["fact_id"], item["revision"]) for item in facts} != references:
                raise ValueError("causal_referenced_fact_unavailable")
        return cls.build(snapshot, previous=previous, previous_source=previous_source,
                         referenced_facts=sorted(facts, key=lambda item: (item["fact_id"], item["revision"])))
