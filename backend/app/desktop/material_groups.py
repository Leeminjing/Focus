"""本文件对外提供 MaterialGroupService，维护用户自定义材料存放位置与顺序。

输入为任务作用域、组名、组或材料 ID、目标位置及确认标记；输出为自定义组和 membership
载荷。具体工作流为按任务串行协调变更，在单个事务中校验任务归属，执行组 CRUD、完整顺序
重排、唯一材料移动和恢复自动归类；删除非空组必须确认，级联仅解除 membership，绝不删除
材料或运行历史。

示例：group = await service.create(task_id, "需求资料")；await service.move(task_id, material_id, group_id, 0)。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.models import DesktopMaterial, DesktopThread, MaterialGroup, MaterialGroupMembership


def _serialize_task_mutation(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @wraps(method)
    async def wrapped(self: MaterialGroupService, task_id: str, *args: Any, **kwargs: Any) -> Any:
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            return await method(self, task_id, *args, **kwargs)

    return wrapped


class MaterialGroupService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._task_locks: dict[str, asyncio.Lock] = {}

    async def list(self, task_id: str) -> list[dict]:
        async with self._session_factory() as session:
            await self._require_task(session, task_id)
            groups = (
                await session.scalars(
                    select(MaterialGroup).where(MaterialGroup.task_id == task_id).order_by(MaterialGroup.position)
                )
            ).all()
            memberships = (
                await session.scalars(
                    select(MaterialGroupMembership)
                    .join(MaterialGroup, MaterialGroup.group_id == MaterialGroupMembership.group_id)
                    .where(MaterialGroup.task_id == task_id)
                    .order_by(MaterialGroupMembership.position)
                )
            ).all()
            by_group: dict[str, list[dict]] = {group.group_id: [] for group in groups}
            for item in memberships:
                by_group[item.group_id].append(self._membership_payload(item))
            return [self._group_payload(group, by_group[group.group_id]) for group in groups]

    @_serialize_task_mutation
    async def create(self, task_id: str, name: str) -> dict:
        async with self._session_factory() as session:
            await self._require_task(session, task_id)
            position = int(
                await session.scalar(
                    select(func.coalesce(func.max(MaterialGroup.position), -1) + 1).where(
                        MaterialGroup.task_id == task_id
                    )
                )
                or 0
            )
            group = MaterialGroup(group_id=uuid.uuid4().hex, task_id=task_id, name=name.strip(), position=position)
            if not group.name:
                raise HTTPException(422, "分组名称不能为空")
            session.add(group)
            await self._commit(session, "同一任务中的分组名称不能重复")
            return self._group_payload(group, [])

    @_serialize_task_mutation
    async def rename(self, task_id: str, group_id: str, name: str) -> dict:
        async with self._session_factory() as session:
            group = await self._require_group(session, task_id, group_id)
            group.name = name.strip()
            if not group.name:
                raise HTTPException(422, "分组名称不能为空")
            await self._commit(session, "同一任务中的分组名称不能重复")
            return self._group_payload(group, await self._members(session, group_id))

    @_serialize_task_mutation
    async def delete(self, task_id: str, group_id: str, confirm: bool) -> None:
        async with self._session_factory() as session:
            group = await self._require_group(session, task_id, group_id)
            count = int(
                await session.scalar(
                    select(func.count()).select_from(MaterialGroupMembership).where(
                        MaterialGroupMembership.group_id == group_id
                    )
                )
                or 0
            )
            if count and not confirm:
                raise HTTPException(409, "非空分组需要确认后删除")
            await session.delete(group)
            await session.flush()
            await self._normalize_groups(session, task_id)
            await session.commit()

    @_serialize_task_mutation
    async def reorder(self, task_id: str, group_ids: list[str]) -> list[dict]:
        async with self._session_factory() as session:
            groups = (
                await session.scalars(select(MaterialGroup).where(MaterialGroup.task_id == task_id))
            ).all()
            if len(group_ids) != len(set(group_ids)) or set(group_ids) != {item.group_id for item in groups}:
                raise HTTPException(422, "分组排序必须完整且不能重复")
            by_id = {item.group_id: item for item in groups}
            for position, group_id in enumerate(group_ids):
                by_id[group_id].position = position
            await session.commit()
        return await self.list(task_id)

    @_serialize_task_mutation
    async def move(
        self,
        task_id: str,
        material_id: str,
        group_id: str | None,
        position: int,
    ) -> dict | None:
        async with self._session_factory() as session:
            material = await session.get(DesktopMaterial, material_id)
            if material is None or material.task_id != task_id:
                raise HTTPException(404, "材料不存在或不属于当前任务")
            existing = await session.get(MaterialGroupMembership, material_id)
            if group_id is None:
                if existing is not None:
                    source_group_id = existing.group_id
                    await session.delete(existing)
                    await session.flush()
                    await self._normalize_members(session, source_group_id)
                    await session.commit()
                return None
            await self._require_group(session, task_id, group_id)
            source_group_id = existing.group_id if existing is not None else None
            if existing is None:
                existing = MaterialGroupMembership(material_id=material_id, group_id=group_id, position=position)
                session.add(existing)
            else:
                existing.group_id = group_id
            await session.flush()
            if source_group_id and source_group_id != group_id:
                await self._normalize_members(session, source_group_id)
            target_members = (
                await session.scalars(
                    select(MaterialGroupMembership)
                    .where(
                        MaterialGroupMembership.group_id == group_id,
                        MaterialGroupMembership.material_id != material_id,
                    )
                    .order_by(MaterialGroupMembership.position, MaterialGroupMembership.material_id)
                )
            ).all()
            target_members.insert(min(position, len(target_members)), existing)
            for target_position, member in enumerate(target_members):
                member.position = target_position
            await session.commit()
            return self._membership_payload(existing)

    @_serialize_task_mutation
    async def reorder_members(self, task_id: str, group_id: str, material_ids: list[str]) -> list[dict]:
        async with self._session_factory() as session:
            await self._require_group(session, task_id, group_id)
            members = (
                await session.scalars(
                    select(MaterialGroupMembership).where(MaterialGroupMembership.group_id == group_id)
                )
            ).all()
            if len(material_ids) != len(set(material_ids)) or set(material_ids) != {item.material_id for item in members}:
                raise HTTPException(422, "组内排序必须完整且不能重复")
            by_id = {item.material_id: item for item in members}
            for position, material_id in enumerate(material_ids):
                by_id[material_id].position = position
            await session.commit()
            return [self._membership_payload(by_id[value]) for value in material_ids]

    async def _members(self, session: AsyncSession, group_id: str) -> list[dict]:
        rows = (
            await session.scalars(
                select(MaterialGroupMembership)
                .where(MaterialGroupMembership.group_id == group_id)
                .order_by(MaterialGroupMembership.position)
            )
        ).all()
        return [self._membership_payload(item) for item in rows]

    async def _normalize_members(self, session: AsyncSession, group_id: str) -> None:
        members = (
            await session.scalars(
                select(MaterialGroupMembership)
                .where(MaterialGroupMembership.group_id == group_id)
                .order_by(MaterialGroupMembership.position, MaterialGroupMembership.material_id)
            )
        ).all()
        for position, member in enumerate(members):
            member.position = position

    async def _normalize_groups(self, session: AsyncSession, task_id: str) -> None:
        groups = (
            await session.scalars(
                select(MaterialGroup)
                .where(MaterialGroup.task_id == task_id)
                .order_by(MaterialGroup.position, MaterialGroup.group_id)
            )
        ).all()
        for position, group in enumerate(groups):
            group.position = position

    async def _require_task(self, session: AsyncSession, task_id: str) -> DesktopThread:
        task = await session.get(DesktopThread, task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return task

    async def _require_group(self, session: AsyncSession, task_id: str, group_id: str) -> MaterialGroup:
        group = await session.get(MaterialGroup, group_id)
        if group is None or group.task_id != task_id:
            raise HTTPException(404, "材料分组不存在或不属于当前任务")
        return group

    @staticmethod
    async def _commit(session: AsyncSession, conflict_message: str) -> None:
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, conflict_message) from error

    @staticmethod
    def _membership_payload(item: MaterialGroupMembership) -> dict:
        return {"material_id": item.material_id, "group_id": item.group_id, "position": item.position}

    @staticmethod
    def _group_payload(group: MaterialGroup, memberships: list[dict]) -> dict:
        return {
            "group_id": group.group_id,
            "task_id": group.task_id,
            "name": group.name,
            "position": group.position,
            "memberships": memberships,
        }
