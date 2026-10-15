"""本文件对外提供 MaterialSnapshotVerifier，核对活动运行的材料内容快照。

输入为数据库会话、不可变 RunMaterialInputs 和工作区路径；输出为内容未变化的原聚合，或为
变化材料补充受保护 Git blob 标识的新聚合。具体工作流为批量读取匹配摘要的 MaterialVersion，
逐项计算当前文件摘要；匹配则沿用当前路径，不匹配但有受保护版本则切换快照来源，否则在自动
投影前以包含材料身份的明确错误停止，绝不静默使用新内容。

工作区受理复用 protect 保存 Git 版本；非 Git 的独占上传保持原文件与摘要，不初始化仓库。
read 只返回匹配冻结摘要的来源字节，materialize 将授权版本投影到执行目录中的独占附件路径。
执行文件以完整内容摘要命名，避免叠加材料身份超过 Windows 路径限制；复用路径仍校验完整摘要。
示例：verified = await verifier.verify(session, inputs, workspace_path)。
"""

from dataclasses import replace
import asyncio
import hashlib
import os
from pathlib import Path
import re
import subprocess
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.material_files import resolve_material_path
from backend.app.desktop.models import MaterialVersion
from focus.agents.material_inputs import RunMaterialInputs


class MaterialSnapshotVerifier:
    async def protect(self, session, material, workspace_path, source, *, expected_digest=None):
        root = Path(workspace_path)
        path = resolve_material_path(workspace_path, material.relative_path)
        digest = await asyncio.to_thread(self._digest, path)
        if expected_digest and digest != expected_digest:
            raise HTTPException(409, "材料摘要与提交版本不一致")
        if not (root / ".git").exists():
            if getattr(material, "workspace_id", None) and material.intake_upload_key:
                return None
            raise HTTPException(409, "材料保护需要已有 Git 仓库")
        object_id = await self._git(root, "hash-object", "-w", "--", str(path))
        raw = await asyncio.to_thread(self._blob, root, object_id)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise HTTPException(409, "材料在版本保护期间发生变化")
        tree = await self._git(root, "mktree", stdin=f"100644 blob {object_id}\tcontent\n")
        args = ["commit-tree", tree, "-m", f"Focus material {material.material_id}: {source}"]
        try:
            parent = await self._git(root, "rev-parse", "--verify", material.git_ref)
            args[2:2] = ["-p", parent]
        except HTTPException:
            pass
        commit = await self._git(root, *args)
        await self._git(root, "update-ref", material.git_ref, commit)
        version = MaterialVersion(version_id=uuid.uuid4().hex, material_id=material.material_id,
            commit_id=commit, object_id=object_id, digest=digest, source=source)
        material.digest = digest
        session.add(version)
        return version

    def read(self, item, workspace_path):
        path = resolve_material_path(workspace_path, item.relative_path)
        raw = self._blob(Path(workspace_path), item.snapshot_object_id) if item.snapshot_object_id else path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item.digest or len(raw) != item.size_bytes:
            raise HTTPException(409, f"材料与冻结版本不一致: {item.material_id}")
        return raw

    def materialize(self, item, source_root, execution_root):
        raw = self.read(item, source_root)
        name = f"frozen-{item.digest}{Path(item.relative_path).suffix}"
        relative = f".focus/attachments/{name}"
        path = resolve_material_path(execution_root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(raw)
        except FileExistsError:
            if self._digest(path) != item.digest:
                raise HTTPException(409, f"执行材料快照路径冲突: {item.material_id}")
        return replace(item, relative_path=relative, snapshot_object_id=None)

    async def _git(self, root, *args, stdin=None):
        environment = {**os.environ, "GIT_AUTHOR_NAME": "Focus", "GIT_AUTHOR_EMAIL": "focus@localhost",
            "GIT_COMMITTER_NAME": "Focus", "GIT_COMMITTER_EMAIL": "focus@localhost"}
        result = await asyncio.to_thread(subprocess.run,
            ["git", "-C", str(root), *args], input=stdin, capture_output=True,
            text=True, timeout=30, check=False, env=environment)
        if result.returncode:
            raise HTTPException(409, result.stderr.strip() or "材料版本保护失败")
        return result.stdout.strip()

    def _blob(self, root, object_id):
        if not re.fullmatch(r"[0-9a-f]{40,64}", object_id):
            raise HTTPException(409, "材料快照身份无效")
        result = subprocess.run(["git", "-C", str(root), "cat-file", "blob", object_id],
            capture_output=True, check=False, timeout=30)
        if result.returncode:
            raise HTTPException(409, "材料受保护版本不可用")
        return result.stdout

    async def verify(
        self,
        session: AsyncSession,
        inputs: RunMaterialInputs,
        workspace_path: str,
    ) -> RunMaterialInputs:
        if not inputs.attached or all(item.digest == "legacy" for item in inputs.attached):
            return inputs
        ids = [item.material_id for item in inputs.attached]
        digests = [item.digest for item in inputs.attached]
        versions = (
            await session.scalars(
                select(MaterialVersion).where(
                    MaterialVersion.material_id.in_(ids),
                    MaterialVersion.digest.in_(digests),
                )
            )
        ).all()
        protected = {(item.material_id, item.digest): item.object_id for item in versions}
        resolved = []
        for item in inputs.attached:
            try:
                path = resolve_material_path(workspace_path, item.relative_path)
                current_digest = self._digest(path) if path.is_file() else None
            except (OSError, ValueError):
                current_digest = None
            if item.snapshot_object_id:
                self.read(item, workspace_path)
                resolved.append(item)
                continue
            if item.digest == "legacy" or current_digest == item.digest:
                resolved.append(item)
                continue
            object_id = protected.get((item.material_id, item.digest))
            if object_id:
                resolved.append(replace(item, snapshot_object_id=object_id))
                continue
            raise HTTPException(
                409,
                f"运行材料已变化且没有可用受保护快照: {item.relative_path} (material_id={item.material_id})",
            )
        return RunMaterialInputs.build(
            inputs.origin_run_id,
            inputs.origin_message_id,
            resolved,
            inputs.required_image_ids,
        )

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
