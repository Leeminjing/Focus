"""Context ownership is application policy; the native host knows none of it.

Bindings are server-owned entries in Context ui_state. A prior connection's IDs
remain tombstones after restart until the user explicitly replaces the binding.
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


class ContextBrowsers:
    def __init__(self, sessions, bridge, executable=None):
        self.sessions, self.bridge = sessions, bridge
        self.transport = BrowserTransport(executable, self._event)
        self.bindings = {}
        self.browsers = {}
        self.runs = set()
        self.lease_owners = {}
        self.changes = ChangeSignal()
        self._creating = {}
        self._context_locks = {}
        self._selections = {}
        self._policy_lock = asyncio.Lock()

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
        return self.browsers[browser_id]

    async def _save(self, context_id, browser):
        async with self.sessions() as session:
            context = await session.scalar(select(DesktopThread).where(DesktopThread.task_id == context_id).with_for_update())
            if context is None:
                raise HTTPException(404, "Context 不存在")
            context.ui_state = {**(context.ui_state or {}), BINDING_KEY: {
                "browser_id": browser.browser_id, "host_id": browser.host_id,
            }}
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
        if browser.host_id != self.transport.host_id or browser.state in FATAL:
            raise BrowserError(browser.state, "浏览器会话已失效；请显式更换绑定后再运行")
        self.transport.check()

    async def snapshot(self, context_id):
        browser = await self.binding(context_id)
        if browser is None:
            return {"context_id": context_id, "state": "unbound", "browser_id": None}
        async with self.sessions() as session:
            users = (await session.scalars(select(DesktopThread.task_id).where(
                DesktopThread.ui_state[BINDING_KEY]["browser_id"].astext == browser.browser_id))).all()
        return {"context_id": context_id, "browser_id": browser.browser_id,
                "state": browser.state, "detail": browser.detail,
                "selected_page_id": browser.selected_page_id, "pages_revision": browser.pages_revision,
                "shared_context_ids": sorted(users)}

    async def pages(self, context_id):
        browser = await self.binding(context_id)
        if browser is None:
            return {"selected_page_id": None, "pages": []}
        self._check(browser)
        return await self.transport.call("list_pages", browser_id=browser.browser_id)

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
        await self.transport.start()
        if selection and self._selections.get(selection.client_id) != selection.sequence:
            return {**await self.snapshot(context_id), "superseded": True}
        params = {"browser_id": browser.browser_id}
        if selection and getattr(selection, "page_id", None):
            params["page_id"] = selection.page_id
        result = await self.transport.call("present", **params)
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
            self.transport = BrowserTransport(self.transport.executable, self._event)
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
                    DesktopThread.ui_state[BINDING_KEY]["browser_id"].astext == old.browser_id).limit(1))
            if not users and old.host_id == self.transport.host_id and old.state not in {"closed", "disconnected"}:
                await self.transport.call("close", browser_id=old.browser_id)
        return await self.snapshot(context_id)

    def resource(self, context_id, run_id, routing=None):
        return BrowserRun(self, context_id, run_id, routing)

    def _event(self, event):
        browser_id = event.get("browser_id")
        affected = list(self.browsers.values()) if event["event"] == "disconnected" else [self.browsers.get(browser_id)]
        if event["event"] == "created" and browser_id not in self.browsers:
            self.browsers[browser_id] = BrowserSession(browser_id, event.get("state", "available"), self.transport.host_id)
        for browser in affected:
            if browser is None:
                continue
            if event["event"] == "disconnected" and browser.state == "closed":
                continue
            if "state" in event:
                browser.state = event["state"]
            browser.detail = event.get("detail", {})
            kind, page_id = event["event"], browser.detail.get("page_id")
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
        if self._creating:
            await asyncio.gather(*self._creating.values(), return_exceptions=True)
        # EOF preserves native windows for the user; never terminate the process.
        await self.transport.disconnect()


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
        self.check()
        self.publish({"event": "bound", "state": self.browser.state})

    def check(self):
        if self.failure:
            raise self.failure
        if self._finished:
            raise BrowserError("run_finished", "Run 已结束")
        if self.browser:
            self.owner._check(self.browser)

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
                await self.owner.transport.call("renew", browser_id=self.browser.browser_id, token=token, ttl_ms=LEASE_TTL_MS)
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
            self._acquisition = asyncio.create_task(self.owner.transport.call(
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
            result = await self.owner.transport.call("observe", **params)
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
            result = await self.owner.transport.call("act", browser_id=self.browser.browser_id, token=self.token,
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
        return await self.owner.transport.call("list_pages", browser_id=self.browser.browser_id)

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
            return await self.owner.transport.call(method, browser_id=self.browser.browser_id,
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
            await self.owner.transport.call("release", browser_id=self.browser.browser_id, token=token)
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
