"""Project trusted file locations from immutable revision lineage, never model text.

Inherited messages keep their original producer. Missing, conflicting or overly
deep lineage yields a visible reason instead of selecting the latest run.
"""
from backend.app.desktop.contexts.evolution import ContextRevisionNotFound
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread


class ConversationFileSources:
    def __init__(self, session, repository, reader, workspace_id):
        self.session, self.repository, self.reader = session, repository, reader
        self.workspace_id = workspace_id
        self.revisions, self.messages, self.resolved, self.run_contexts = {}, {}, {}, {}

    @staticmethod
    def unavailable():
        return {"error": "回答缺少可验证的文件执行来源；请从文件 / 产物打开"}

    async def for_message(self, revision, message, ancestors=()):
        identity = (revision.ref.revision_id, str(message.get("id")))
        if identity in self.resolved:
            return self.resolved[identity]
        if not message.get("id") or identity[0] in ancestors or len(ancestors) >= 32:
            return self.unavailable()
        inherited = []
        try:
            for source in revision.sources:
                key = source.source.revision_id
                if key not in self.revisions:
                    if len(self.revisions) >= 64:
                        return self.unavailable()
                    self.revisions[key] = await self.repository.get_by_id(self.session, key)
                previous = self.revisions[key]
                # Settled revisions persist the full checkpoint message ID set.
                # New messages need no deserialization of inherited histories.
                initial_ids = getattr(previous, "initial_message_ids", ())
                if previous.origin_kind == "run_settled" and initial_ids and str(message["id"]) not in initial_ids:
                    continue
                if key not in self.messages:
                    view = await self.reader.read(self.session, previous.ref, "display")
                    self.messages[key] = {str(item.get("id")): item for item in view.messages if item.get("id")}
                original = self.messages[key].get(str(message["id"]))
                if original and original.get("content") == message.get("content") and (
                    original.get("role") or original.get("type")
                ) in {"ai", "assistant"}:
                    inherited.append(await self.for_message(previous, original, (*ancestors, identity[0])))
        except ContextRevisionNotFound:
            return self.unavailable()
        if inherited:
            result = inherited[0] if all(item == inherited[0] for item in inherited) else self.unavailable()
        elif revision.origin_kind == "run_settled" and revision.origin_id:
            key = (revision.origin_id, revision.ref.context_id)
            if key not in self.run_contexts:
                run = await self.session.get(DesktopRun, revision.origin_id)
                task = await self.session.get(DesktopThread, run.task_id) if run else None
                result = self.unavailable()
                if run is not None and run.task_id == revision.ref.context_id and task is not None and task.workspace_id == self.workspace_id:
                    result = {"task_id": run.task_id, "workspace_id": task.workspace_id, "run_id": run.run_id}
                    slot = (run.workspace_anchor or {}).get("slot_id")
                    if slot:
                        result["slot_id"] = slot
                self.run_contexts[key] = result
            result = self.run_contexts[key]
        else:
            result = self.unavailable()
        self.resolved[identity] = result
        return result
