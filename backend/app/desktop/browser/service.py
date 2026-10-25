"""Context ownership is application policy; the native host knows none of it.

Bindings are server-owned entries in Context ui_state. Opt-in recovery restores
only authenticated live hosts; legacy/missing sessions remain tombstones.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.persistence.state_changes import ChangeSignal
from focus.runtime.runs.events import build_envelope
from focus.runtime.stream_bridge.schemas import StreamEvent
from .transport import BrowserError, BrowserTransport
from .host_artifact import recovery_enabled

BINDING_KEY = "_distinct_browser"
STOPPED = {"taking_over", "manual", "paused", "faulted", "closed", "disconnected"}
FATAL = {"faulted", "closed", "disconnected"}
LEASE_TTL_MS = 30000
RENEW_INTERVAL_SECONDS = 10


@dataclass
class BrowserSession:
    browser_id: str
    state: str
    host_id: str
    hwnd: str = ""
    detail: dict = field(default_factory=dict)
    epoch: int = 0
    selected_page_id: str | None = None
    pages_revision: int = 0
    selection_revision: int = 0
    interactions_revision: int = 0
    downloads: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    rejected_downloads: list = field(default_factory=list)
    recovered: bool = False
    can_reconnect: bool = False
    interrupted_operation: dict = field(default_factory=dict)

    def site_interactions(self):
        downloads, permissions = list(self.downloads.values()), list(self.permissions.values())
        return {"downloads": downloads, "permissions": permissions, "rejected": list(self.rejected_downloads),
                "interaction_pending": any(item.get("state") == "in_progress" for item in downloads) or
                    any(item.get("state") == "pending" for item in permissions)}

    def site_event(self, kind, detail):
        if kind == "download_rejected":
            self.rejected_downloads = [*self.rejected_downloads[-7:], dict(detail)]
        elif kind in {"download_changed", "permission_requested", "permission_resolved"}:
            download = kind == "download_changed"
            key = detail.get("download_id" if download else "request_id")
            if not key:
                return
            records, limit, pending = (self.downloads, 32, "in_progress") if download else (self.permissions, 16, "pending")
            records[key] = {**records.get(key, {}), **detail}
            for previous, record in list(records.items()):
                if len(records) <= limit:
                    break
                if record.get("state") != pending:
                    records.pop(previous)
        else:
            return
        self.interactions_revision += 1


class ContextBrowsers:
    def __init__(self, sessions, bridge, executable=None, *, recovery=None, cache_root=None):
        self.sessions, self.bridge = sessions, bridge
        self.recovery = recovery_enabled() if recovery is None else recovery
        self.executable, self.cache_root = executable, cache_root
        self.transports = {}
        self.transport = self._new_transport()
        self._restore_locks, self._restore_attempted = {}, set()
        self._metadata_tasks = {}
        self.bindings = {}
        self.browsers = {}
        self.runs = set()
        self.lease_owners = {}
        self.changes = ChangeSignal()
        self._creating = {}
        self._context_locks = {}
        self._selections = {}
        self._policy_lock = asyncio.Lock()

    def _new_transport(self, **options):
        source = BrowserTransport(self.executable, lambda event: self._event(event, source),
                                  recovery=self.recovery, cache_root=self.cache_root, **options)
        return source

    def transport_for(self, browser):
        source = self.transports.get(browser.host_id)
        if source is None and browser.host_id == self.transport.host_id:
            source = self.transport
        if source is None:
            raise BrowserError('host_unrecoverable', '原浏览器宿主未连接；没有创建替代页面')
        return source

    async def start(self):
        if not self.recovery: return
        async with self.sessions() as session:
            contexts = (await session.scalars(select(DesktopThread).where(
                DesktopThread.ui_state[BINDING_KEY].astext.is_not(None)))).all()
            identities = [context.task_id for context in contexts]
        for context_id in identities:
            await self.binding(context_id)  # Per-host lock/attempt prevents duplicate clients.

    async def _restore(self, saved, *, retry=False):
        host_id = saved['host_id']
        async with self._restore_locks.setdefault(host_id, asyncio.Lock()):
            if host_id in self._restore_attempted and not retry: return
            self._restore_attempted.add(host_id)
            source = self.transports.get(host_id)
            if source is None or source.failure:
                if source: await source.disconnect()
                source = self._new_transport(endpoint=saved['endpoint'], expected_host_id=host_id,
                                             artifact_sha256=saved['artifact_sha256'])
                self.transports[host_id] = source
            try:
                await source.start()
                # The host_snapshot callback installs state before buffered
                # events. Never apply this stale snapshot again after awaits.
                if self.transport.pid is None and not self.transport.failure:
                    self.transport = source
                browser = self.browsers[saved['browser_id']]
                if not any(item['browser_id'] == browser.browser_id for item in source.recovery_snapshot['browsers']):
                    browser.state = 'closed'
                    browser.detail = {'reason': 'browser_missing', 'message': '原浏览器已关闭；未创建替代页面'}
            except BrowserError as error:
                for browser in self.browsers.values():
                    if browser.host_id == host_id:
                        browser.state, browser.detail = 'disconnected', {'reason': error.code, 'message': str(error)}
            self.changes.publish()

    async def recover(self, context_id):
        async with self._policy_lock:
            saved = await self._saved(context_id)
            if not self.recovery or saved.get('transport') != 'named_pipe':
                raise BrowserError('host_unrecoverable', '此绑定不支持恢复；旧版会话不会自动升级')
            await self.binding(context_id)
            source = self.transports.get(saved['host_id'])
            if source is None or source.failure:
                await self._restore(saved, retry=True)
            return await self.snapshot(context_id)

    async def _saved(self, context_id):
        async with self.sessions() as session:
            context = await session.get(DesktopThread, context_id)
            if context is None:
                raise HTTPException(404, "Context 不存在")
            return dict((context.ui_state or {}).get(BINDING_KEY) or {})

    async def binding(self, context_id):
        saved = await self._saved(context_id)
        if not saved:
            return None
        browser_id = saved["browser_id"]
        self.bindings[context_id] = browser_id
        if browser_id not in self.browsers:
            self.browsers[browser_id] = BrowserSession(browser_id, "disconnected", saved["host_id"])
            self.browsers[browser_id].detail = {'reason': 'legacy_session' if saved.get('transport') != 'named_pipe' else 'recovery_disabled'}
            source = self.transports.get(saved['host_id'])
            if source and source.failure:
                self.browsers[browser_id].detail = {'reason': source.failure.code, 'message': str(source.failure)}
            elif source and source.recovery_snapshot is not None:
                self.browsers[browser_id].state = 'closed'
                self.browsers[browser_id].detail = {'reason': 'browser_missing', 'message': '原浏览器已关闭；没有重建页面'}
        browser = self.browsers[browser_id]
        if browser.host_id != saved['host_id']:
            raise BrowserError('binding_identity_mismatch', '浏览器绑定的宿主身份不一致')
        browser.can_reconnect = bool(self.recovery and saved.get('transport') == 'named_pipe')
        if browser.can_reconnect and saved.get('endpoint') and saved.get('artifact_sha256'):
            await self._restore(saved)
        return self.browsers[browser_id]

    async def _save(self, context_id, browser):
        async with self.sessions() as session:
            context = await session.scalar(select(DesktopThread).where(DesktopThread.task_id == context_id).with_for_update())
            if context is None:
                raise HTTPException(404, "Context 不存在")
            source = self.transport_for(browser)
            saved = {"browser_id": browser.browser_id, "host_id": browser.host_id}
            if source.recovery:
                saved.update(transport='named_pipe', endpoint=source.endpoint, artifact_sha256=source.artifact_sha256)
            context.ui_state = {**(context.ui_state or {}), BINDING_KEY: saved}
            await session.commit()
        self.bindings[context_id] = browser.browser_id
        self.changes.publish()

    async def ensure(self, context_id):
        # Shield the *binding transaction*, not just the create RPC. Cancellation
        # cannot leave a created window untracked, or let a second Run recreate it.
        if context_id not in self._creating:
            task = asyncio.create_task(self._ensure(context_id))
            self._creating[context_id] = task
            def done(result):
                self._creating.pop(context_id, None)
                if not result.cancelled():
                    result.exception()
            task.add_done_callback(done)
        return await asyncio.shield(self._creating[context_id])

    async def _ensure(self, context_id):
        async with self._context_locks.setdefault(context_id, asyncio.Lock()):
            return await self._ensure_locked(context_id)

    async def _ensure_locked(self, context_id):
        browser = await self.binding(context_id)
        if browser is not None:
            self._check(browser)
            return browser
        result = await self.transport.call("create", label=f"Focus · {context_id}"[:200])
        browser = self.browsers.get(result["browser_id"])
        if browser is None:
            browser = BrowserSession(result["browser_id"], result["state"], self.transport.host_id, result["hwnd"])
            self.browsers[browser.browser_id] = browser
        browser.hwnd = result["hwnd"]
        browser.selected_page_id = result["page_id"]
        try:
            await self._save(context_id, browser)
        except BaseException:
            await self.transport.call("close", browser_id=browser.browser_id)
            raise
        self._check(browser)
        return browser

    def _check(self, browser):
        if browser.state in FATAL:
            raise BrowserError(browser.state, "浏览器会话已失效；请显式更换绑定后再运行")
        self.transport_for(browser).check()

    async def snapshot(self, context_id):
        browser = await self.binding(context_id)
        if browser is None:
            return {"context_id": context_id, "state": "unbound", "browser_id": None}
        async with self.sessions() as session:
            users = (await session.scalars(select(DesktopThread.task_id).where(
                DesktopThread.ui_state[BINDING_KEY]["browser_id"].astext == browser.browser_id,
                DesktopThread.ui_state[BINDING_KEY]["host_id"].astext == browser.host_id))).all()
        return {"context_id": context_id, "browser_id": browser.browser_id,
                "state": browser.state, "detail": browser.detail,
                "selected_page_id": browser.selected_page_id, "pages_revision": browser.pages_revision,
                "interactions_revision": browser.interactions_revision, "site_interactions": browser.site_interactions(),
                "recovered": browser.recovered, "can_reconnect": browser.can_reconnect,
                "interrupted_operation": browser.interrupted_operation,
                "shared_context_ids": sorted(users)}

    async def pages(self, context_id):
        browser = await self.binding(context_id)
        if browser is None:
            return {"selected_page_id": None, "pages": []}
        self._check(browser)
        return await self.transport_for(browser).call("list_pages", browser_id=browser.browser_id)

    async def downloads(self, context_id):
        return await self._downloads(await self.binding(context_id))

    async def _downloads(self, browser):
        if browser is None:
            return {"downloads": [], "permissions": [], "rejected": [], "interaction_pending": False}
        self._check(browser)
        result = await self.transport_for(browser).call("list_downloads", browser_id=browser.browser_id)
        # Permission decisions arrive through events. A late RPC cannot clear
        # a newer wait; report pending conservatively until the next query.
        interactions = browser.site_interactions()
        return {**interactions, **result,
                "interaction_pending": bool(result["interaction_pending"] or interactions["interaction_pending"])}

    async def present(self, context_id, selection=None):
        if selection:
            previous = self._selections.get(selection.client_id, 0)
            self._selections[selection.client_id] = max(previous, selection.sequence)
            if len(self._selections) > 128:
                self._selections.pop(next(iter(self._selections)))
        browser = await self.binding(context_id)
        if browser is None:
            return await self.snapshot(context_id)
        self._check(browser)
        source = self.transport_for(browser)
        await source.start()
        if selection and self._selections.get(selection.client_id) != selection.sequence:
            return {**await self.snapshot(context_id), "superseded": True}
        params = {"browser_id": browser.browser_id}
        if selection and getattr(selection, "page_id", None):
            params["page_id"] = selection.page_id
        result = await source.call("present", **params)
        # Never clear a pause here. Only the native handback opens a new lease.
        return {**result, **await self.snapshot(context_id)}

    async def replace(self, context_id, source_context_id=None):
        async with self._policy_lock:
            async with self._context_locks.setdefault(context_id, asyncio.Lock()):
                return await self._replace_locked(context_id, source_context_id)

    async def _replace_locked(self, context_id, source_context_id):
        if source_context_id == context_id:
            raise HTTPException(409, "共享来源必须是另一个 Context")
        async with self.sessions() as session:
            target = await session.get(DesktopThread, context_id)
            source = await session.get(DesktopThread, source_context_id) if source_context_id else None
            if target is None or (source_context_id and source is None):
                raise HTTPException(404, "Context 不存在")
            if source and source.workspace_id != target.workspace_id:
                raise HTTPException(409, "只能共享同一工作区的浏览器")
            active = await session.scalar(select(DesktopRun.run_id).where(
                DesktopRun.task_id == context_id, DesktopRun.status.in_(("pending", "running"))).limit(1))
            if active or context_id in self._creating or any(run.context_id == context_id for run in self.runs):
                raise HTTPException(409, "请先结束该 Context 的 Run 再更换浏览器绑定")
        old = await self.binding(context_id)
        if self.transport.failure:
            # Explicit replacement starts a new connection, never reconnects
            # old IDs/tokens or claims the retained manual windows as new.
            self.transport = self._new_transport()
        if source_context_id:
            browser = await self.ensure(source_context_id)
            await self._save(context_id, browser)
        else:
            result = await self.transport.call("create", label=f"Focus · {context_id}"[:200])
            browser = self.browsers.get(result["browser_id"]) or BrowserSession(
                result["browser_id"], result["state"], self.transport.host_id, result["hwnd"])
            self.browsers[browser.browser_id] = browser
            browser.hwnd = result["hwnd"]
            browser.selected_page_id = result["page_id"]
            try:
                await self._save(context_id, browser)
            except BaseException:
                await self.transport.call("close", browser_id=browser.browser_id)
                raise
        if old and old.browser_id != browser.browser_id:
            # Query durable references, including Contexts not loaded this boot.
            async with self.sessions() as session:
                users = await session.scalar(select(DesktopThread.task_id).where(
                    DesktopThread.ui_state[BINDING_KEY]["browser_id"].astext == old.browser_id,
                    DesktopThread.ui_state[BINDING_KEY]["host_id"].astext == old.host_id).limit(1))
            if not users and old.state not in {"closed", "disconnected"}:
                await self.transport_for(old).call("close", browser_id=old.browser_id)
        return await self.snapshot(context_id)

    def resource(self, context_id, run_id, routing=None):
        return BrowserRun(self, context_id, run_id, routing)

    async def _refresh_interrupted(self, browser, source):
        try:
            snapshot = await source.call('snapshot')
            if source.failure or self.transports.get(browser.host_id) is not source: return
            item = next((item for item in snapshot['browsers'] if item['browser_id'] == browser.browser_id), None)
            if item:
                # Only refresh the old operation's outcome. Reapplying page or
                # control state here would overwrite newer events/observations.
                browser.interrupted_operation = item.get('interrupted_operation', {})
                self.changes.publish()
        except BrowserError:
            pass
        finally:
            self._metadata_tasks.pop(browser.browser_id, None)

    def _event(self, event, source=None):
        source = source or self.transport
        self.transports[source.host_id] = source
        if event['event'] == 'host_snapshot':
            self._restore_attempted.add(source.host_id)
            for item in event['detail']['browsers']:
                browser = self.browsers.get(item['browser_id'])
                if browser is None:
                    browser = self.browsers[item['browser_id']] = BrowserSession(item['browser_id'], item['state'], source.host_id)
                if browser.host_id != source.host_id: continue
                browser.state = item['state']
                browser.detail = {'reason': 'host_reconnected'} if event.get('restored') else {}
                browser.recovered, browser.can_reconnect = event.get('restored', False), True
                browser.selected_page_id = item['selected_page_id']
                browser.hwnd = next((page['hwnd'] for page in item['pages'] if page['page_id'] == item['selected_page_id']), '')
                browser.interrupted_operation = item.get('interrupted_operation', {})
                sites = item.get('site_interactions', {})
                browser.downloads = {record['download_id']: record for record in sites.get('downloads', [])}
                browser.permissions = {record['request_id']: record for record in sites.get('permissions', [])}
                browser.epoch += 1; browser.pages_revision += 1; browser.interactions_revision += 1
            self.changes.publish(); return
        browser_id = event.get("browser_id")
        affected = [browser for browser in self.browsers.values() if browser.host_id == source.host_id] if event["event"] == "disconnected" else [self.browsers.get(browser_id)]
        if event["event"] == "created" and browser_id not in self.browsers:
            self.browsers[browser_id] = BrowserSession(browser_id, event.get("state", "available"), source.host_id)
        for browser in affected:
            if browser is None or browser.host_id != source.host_id:
                continue
            if event["event"] == "disconnected" and browser.state == "closed":
                continue
            if "state" in event:
                browser.state = event["state"]
            browser.detail = event.get("detail", {})
            kind, page_id = event["event"], browser.detail.get("page_id")
            browser.site_event(kind, browser.detail)
            if (source.recovery and browser.interrupted_operation and not browser.interrupted_operation.get('settled')
                    and browser.state in {'manual', 'faulted'} and browser.browser_id not in self._metadata_tasks):
                self._metadata_tasks[browser.browser_id] = asyncio.create_task(self._refresh_interrupted(browser, source))
            if kind == "page_selected":
                browser.selected_page_id = page_id
                browser.selection_revision += 1
            if kind in {"page_created", "page_selected", "page_closed", "navigation_completed"}:
                browser.pages_revision += 1
            if kind == "page_closed" and page_id == browser.selected_page_id:
                browser.selected_page_id = None
                browser.selection_revision += 1
            invalidated = browser.state in STOPPED or kind in {"control_changed", "page_selected"}
            if browser.state in STOPPED or kind == "control_changed":
                browser.epoch += 1
            for run in tuple(self.runs):
                if run.browser is browser:
                    page_changed = kind in {"navigation_completed", "page_closed"} and (
                        not page_id or run.observation and run.observation.get("page_id") == page_id)
                    run.changed(event, invalidated or page_changed)
        self.changes.publish()

    async def close(self):
        for task in tuple(self._metadata_tasks.values()): task.cancel()
        await asyncio.gather(*tuple(self._metadata_tasks.values()), return_exceptions=True)
        if self._creating:
            await asyncio.gather(*self._creating.values(), return_exceptions=True)
        # EOF preserves native windows for the user; never terminate the process.
        await asyncio.gather(*(source.disconnect() for source in set(self.transports.values()) | {self.transport}))


class BrowserRun:
    def __init__(self, owner, context_id, run_id, routing=None):
        self.owner, self.context_id, self.run_id = owner, context_id, run_id
        self.routing = routing
        self.browser = None
        self.token = None
        self.observation = None
        self.failure = None
        self._renewal = None
        self._acquisition = None
        self._inflight = False
        self._finished = False
        self.handoff = False

    async def start(self):
        self._task = asyncio.current_task()
        self.owner.runs.add(self)
        self.browser = await self.owner.ensure(self.context_id)
        self.handoff = self.browser.recovered or self.browser.state in {'manual', 'taking_over', 'paused'}
        self.check()
        self.publish({"event": "bound", "state": self.browser.state})

    def check(self):
        if self.failure:
            raise self.failure
        if self._finished:
            raise BrowserError("run_finished", "Run 已结束")
        if self.browser:
            self.owner._check(self.browser)

    @property
    def transport(self):
        return self.owner.transport_for(self.browser)

    def publish(self, event):
        routing = self.routing
        self.owner.bridge.publish(self.run_id, StreamEvent(id="", event="browser", data=build_envelope(
            routing.workspace_id if routing else None, routing.thread_id if routing else None,
            routing.agent_id if routing else None, self.run_id, "browser", {**event, "context_id": self.context_id,
                "browser_id": self.browser.browser_id if self.browser else None})))

    def changed(self, event, invalidated):
        if invalidated:
            self.observation = None
        if self.browser.state != "automatic":
            self.token = None
            if self.owner.lease_owners.get(self.browser.browser_id) is self:
                self.owner.lease_owners.pop(self.browser.browser_id, None)
            if self._renewal:
                self._renewal.cancel()
        if self.browser.state in {"manual", "taking_over", "paused"}:
            self.handoff = True
        self.publish(event)
        if self.browser.state in FATAL and not self._finished:
            self.failure = BrowserError(self.browser.state, "浏览器已关闭、故障或断连；已发动作可能仍在收尾", self.browser.detail)
            if self._task and not self._task.done():
                self._task.cancel()

    async def _renew(self):
        try:
            while self.token:
                await asyncio.sleep(RENEW_INTERVAL_SECONDS)
                token = self.token
                await self.transport.call("renew", browser_id=self.browser.browser_id, token=token, ttl_ms=LEASE_TTL_MS)
        except BrowserError as error:
            self.token, self.observation = None, None
            self.publish({"event": "lease_lost", "state": self.browser.state, "detail": error.payload()})

    async def _ensure_lease(self):
        if self.browser.state in STOPPED:
            raise BrowserError("control_unavailable", "请在原生浏览器中恢复窗口并点击交还，然后重新观察")
        if not self.token:
            # Lease acquisition waits for Focus's current holder. No
            # action queue survives a handoff. Native busy still means reject.
            while (holder := self.owner.lease_owners.get(self.browser.browser_id)) is not None and holder is not self:
                self.publish({"event": "waiting_for_control", "state": self.browser.state})
                generation = self.owner.changes.generation
                await self.owner.changes.wait(generation, timeout=30)
                self.check()
                if self.browser.state in STOPPED:
                    raise BrowserError("control_unavailable", "等待期间用户已接管或暂停，请重新观察")
            self.owner.lease_owners[self.browser.browser_id] = self
            epoch = self.browser.epoch
            self._acquisition = asyncio.create_task(self.transport.call(
                "acquire", browser_id=self.browser.browser_id, client_id=self.run_id, ttl_ms=LEASE_TTL_MS))
            lease = await asyncio.shield(self._acquisition)
            self._acquisition = None
            # acquire itself emits control_changed(automatic). A subsequent
            # takeover must still defeat its late response.
            if self.browser.state != "automatic" or self.browser.epoch > epoch + 1:
                raise BrowserError("invalid_token", "申请期间控制权已改变，请重新观察")
            self.token = lease["token"]
            self._renewal = asyncio.create_task(self._renew())

    async def observe(self, page_id=None):
        self.check()
        if self._inflight:
            raise BrowserError("busy", "该 Run 已有浏览器调用在途，请求未接受")
        self._inflight = True
        try:
            await self._ensure_lease()
            token = self.token
            revision = self.browser.selection_revision
            params = {"browser_id": self.browser.browser_id, "token": token}
            if page_id is not None:
                params["page_id"] = page_id
            result = await self.transport.call("observe", **params)
            # The native capture validates navigation/viewport revisions. A
            # navigation_completed event from the preceding act may arrive while
            # this fresh capture is pending; it does not invalidate its result.
            if self.token != token or self.browser.selection_revision != revision:
                raise BrowserError("page_changed", "观察期间控制权已变化，请重新观察")
            if not result.get("page_id") or page_id is not None and result["page_id"] != page_id:
                raise BrowserError("protocol_mismatch", "观察结果缺少匹配的页面标识")
            self.observation = {**result, "observation_id": uuid.uuid4().hex}
            return self.observation
        except (BrowserError, asyncio.CancelledError) as error:
            self.observation = None
            if isinstance(error, BrowserError) and error.code in {"invalid_token", "control_unavailable", "window_paused"}:
                self.token = None
            if not self.token and self.owner.lease_owners.get(self.browser.browser_id) is self:
                self.owner.lease_owners.pop(self.browser.browser_id, None)
                self.owner.changes.publish()
            raise
        finally:
            self._inflight = False

    async def act(self, observation_id, action, coordinate_space="image"):
        self.check()
        if self._inflight:
            raise BrowserError("busy", "该 Run 已有浏览器调用在途，请求未接受")
        observed = self.observation
        if not self.token or not observed or observed["observation_id"] != observation_id:
            raise BrowserError("observation_required", "请先 browser_observe；禁止继续旧观察、旧令牌或旧动作队列")
        if coordinate_space not in {"image", "css_viewport"}:
            raise BrowserError("invalid_action", "coordinate_space 必须是 image 或 css_viewport")
        action = dict(action)
        if "x" in action and coordinate_space == "image":
            for axis, size in (("x", "width"), ("y", "height")):
                action[axis] *= observed["viewport"][size] / observed["image"][size]
        self._inflight = True
        try:
            result = await self.transport.call("act", browser_id=self.browser.browser_id, token=self.token,
                page_id=observed["page_id"], action=action)
            if action["type"] == "navigate" or result.get("control_still_valid") is False:
                self.observation = None
            return result
        except BrowserError:
            # Preserve the native error, including result_unknown; never replay.
            self.observation = None
            raise
        finally:
            self._inflight = False

    async def list_pages(self):
        self.check()
        return await self.transport.call("list_pages", browser_id=self.browser.browser_id)

    async def list_downloads(self):
        self.check()
        return await self.owner._downloads(self.browser)

    async def page_command(self, method, page_id):
        self.check()
        if method not in {"select_page", "close_page"}:
            raise ValueError("unsupported page command")
        if self._inflight:
            raise BrowserError("busy", "该 Run 已有浏览器调用在途，请求未接受")
        self._inflight = True
        try:
            await self._ensure_lease()
            # Page management never grants an observation. Even an uncertain
            # result requires a fresh list/observe, never an automatic replay.
            self.observation = None
            return await self.transport.call(method, browser_id=self.browser.browser_id,
                                                   token=self.token, page_id=page_id)
        except BrowserError as error:
            if error.code in {"invalid_token", "control_unavailable", "window_paused"}:
                self.token = None
            raise
        finally:
            self._inflight = False
            if not self.token and self.owner.lease_owners.get(self.browser.browser_id) is self:
                self.owner.lease_owners.pop(self.browser.browser_id, None)
                self.owner.changes.publish()

    async def release(self):
        token, self.token = self.token, None
        self.observation = None
        if self._renewal:
            self._renewal.cancel()
            await asyncio.gather(self._renewal, return_exceptions=True)
            self._renewal = None
        if token:
            await self.transport.call("release", browser_id=self.browser.browser_id, token=token)
        if self.browser and self.owner.lease_owners.get(self.browser.browser_id) is self:
            self.owner.lease_owners.pop(self.browser.browser_id, None)
            self.owner.changes.publish()
        return {"released": True, "page_retained": True}

    async def finish(self):
        self._finished = True
        self.owner.runs.discard(self)
        try:
            if self._acquisition:
                lease = await asyncio.shield(self._acquisition)
                self.token = lease["token"]
                self._acquisition = None
            await self.release()
        except BrowserError:
            pass  # Revocation/EOF already invalidated this Run's token.
