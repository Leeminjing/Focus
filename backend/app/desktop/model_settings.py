"""
本文件对外提供桌面模型设置域能力：读模型、写入前校验、保存并立即生效、连通性测试与条目引用查询。

对外提供:
    CONFIG_FILE_NAME — 文件层配置文件名（与 get_app_config("config.yaml") 的既有约定一致）
    SUPPORTED_ADAPTERS — 设置界面可选的适配器集合
    ModelSettingsError — 校验失败，携带按字段定位的错误
    credential_variable(entry) — 条目凭据来源变量名
    catalog_snapshot(app_config) — 生效目录的完整读模型（含来源标注，MUST NOT 含凭据值）
    settings_snapshot(app_config) — 设置页所需的完整快照（目录 + 删除名单 + 适配器 + 默认解析状态）
    validate_entry(entry) / validate_catalog(models) — 单条目与目录级结构校验
    save_model_settings(app_config, payload) — 校验 → 落盘 → 就地生效，返回新快照
    probe_model_connection(entry, api_key_value) — 用候选条目发起一次最小真实调用
    referencing_models(session_factory, names) — 查询引用这些条目名的任务与运行

输入:
    app_config: AppConfig — 运行中按引用持有的权威配置对象（就地更新的目标）
    payload: dict — 界面提交的 {models, removed_models, credentials}
    names: Sequence[str] — 待查询引用关系的条目名

输出:
    catalog_snapshot → list[dict]；settings_snapshot / save_model_settings → dict
    probe_model_connection → {"ok": bool, "reason": str}；referencing_models → dict[str, list[dict]]

凭据边界：读模型里的 api_key 一律取自**未经环境变量解析的原始声明**，因此它要么是 `$VAR`
形式的引用，要么是空串（条目用手写字面量密钥时只标记 api_key_literal，绝不回显值）。
配置对象上的 api_key 在加载期已被 resolve_env_vars 解析成明文，直接读它会泄漏凭据。

具体工作流（保存）:
    (1) 字段与目录级校验：未知字段、必填、条目名唯一、适配器在受支持集合内、端点 http/https、
        窗口与策展上限为正整数、恰好一个默认与一个策展默认、策展默认必须声明策展输出方式。
    (2) 凭据可用性：每个条目的来源变量必须能在本次提交值或当前进程内取到。
    (3) 内存中构造一份校验通过的 AppConfig（待提交凭据就地代入）并装配自检——失败都不落盘。
    (4) 依次写入凭据文件、用户偏好层配置，最后就地把新对象应用到权威对象，使全部持有者立刻
        读到新值。删除名单由服务端从期望目录与文件层的差集推导，不依赖界面自报。

为什么默认与策展默认要求「恰好一个」而不是「至多一个」：保存是人明确表态「系统该用哪个模型」
的动作，留空等于把下一次运行交给一条已知会失败的解析路径（本变更的触发场景就是这类报错）。
载入期仍保留既有的「零声明可加载」语义——手工编辑配置文件的人可能有意用环境变量选模型。

为什么适配器是受支持集合而不是自由文本：`use` 是 resolve_class 的导入路径，允许自由填写等于
把本机设置界面变成任意导入入口。端点（base_url）允许自填，因为自建与代理是正常诉求。

示例:
    snapshot = save_model_settings(app_config, {"models": [...], "removed_models": ["legacy"]})
    result = await probe_model_connection(entry, api_key_value="sk-...")
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from focus.config.app_config import AppConfig, apply_app_config, build_app_config
from focus.config.env import match_env_ref
from focus.config.layered import load_layered_maps
from focus.config.model_config import ModelConfig
from focus.config.model_layers import derive_preference_overlay, merge_model_layers
from focus.config.preference_store import (
    PreferenceWriteError,
    apply_credential,
    credential_is_set,
    write_models_preference,
)

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.yaml"
"""文件层配置文件名：与既有 `get_app_config("config.yaml")` 调用约定一致（cwd 相对）。"""

SUPPORTED_ADAPTERS: tuple[dict[str, str], ...] = (
    {
        "use": "focus.models.deepseek:DeepSeekChatOpenAI",
        "label": "DeepSeek（OpenAI 兼容）",
    },
    {
        "use": "langchain_openai:ChatOpenAI",
        "label": "OpenAI 兼容（通用）",
    },
)
"""设置界面可选的适配器；界面只允许在这些之间选择，避免把界面变成任意导入入口。"""

_CURATION_OUTPUT_METHODS = ("json_schema", "json_mode", "prompt_json")

_REQUIRED_STRING_FIELDS = ("name", "display_name", "use", "model", "api_key", "base_url")

_EDITABLE_FIELDS = (
    "name",
    "display_name",
    "use",
    "model",
    "api_key",
    "base_url",
    "context_window",
    "curation_output_method",
    "curation_max_output_tokens",
    "curation_default",
    "default",
    "supports_image_input",
)
"""设置界面可编辑的全部参数，与 ModelConfig 的字段一致。"""


class ModelSettingsError(ValueError):
    """设置校验失败：携带按字段定位的错误，供界面就地呈现。"""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{field}: {message}" for field, message in errors.items()))
        self.errors = errors


def _redact(text: str, secrets: Sequence[str]) -> str:
    """从错误信息中抹掉待提交的凭据值，避免它出现在响应或日志里。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _entry_name(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    return name if isinstance(name, str) else None


def _by_name(entries: Sequence[Any]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for entry in entries:
        name = _entry_name(entry)
        if name is not None and isinstance(entry, dict):
            result[name] = entry
    return result


def _normalized_entry(entry: Any) -> dict | None:
    """把一份原始条目收敛为声明式完整条目（不做环境变量解析）；无法校验时返回 None。"""
    if not isinstance(entry, dict):
        return None
    try:
        return ModelConfig.model_validate(entry).model_dump()
    except ValidationError:
        return None


def credential_variable(entry: dict) -> str | None:
    """条目凭据来源变量名；条目用字面量密钥时返回 None。"""
    return match_env_ref(entry.get("api_key") or "")


def _raw_layers(config_name: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """读取 (文件层条目, 生效的原始声明条目)。

    两者都未做环境变量解析，因此 `api_key` 仍是指向变量名的引用（或手写字面量），
    不会像配置对象那样已经变成明文。
    """
    file_map, preference_map = load_layered_maps(config_name, str(Path(config_name)))
    file_entries = _by_name(file_map.get("models") or [])
    declared = _by_name(merge_model_layers(file_map, preference_map).get("models") or [])
    return file_entries, declared


def _entry_view(model: ModelConfig, raw: dict | None) -> dict:
    """单个条目的读模型：全部可编辑参数 + 凭据状态（绝不回显凭据值）。"""
    declared = _normalized_entry(raw)
    surface = declared or model.model_dump()
    entry = {field: surface.get(field) for field in _EDITABLE_FIELDS}

    raw_key = raw.get("api_key") if isinstance(raw, dict) else None
    raw_key = raw_key if isinstance(raw_key, str) else ""
    variable = match_env_ref(raw_key)
    entry["api_key"] = raw_key if variable else ""
    entry["api_key_variable"] = variable
    entry["api_key_literal"] = bool(raw_key) and variable is None
    entry["api_key_set"] = credential_is_set(variable) if variable else entry["api_key_literal"]
    return entry


def catalog_snapshot(app_config: AppConfig, config_name: str = CONFIG_FILE_NAME) -> list[dict]:
    """生效目录的完整读模型：全部可编辑参数 + 来源标注 + 凭据状态。"""
    file_entries, declared = _raw_layers(config_name)
    snapshot: list[dict] = []
    for model in app_config.models:
        raw = declared.get(model.name)
        entry = _entry_view(model, raw)
        file_norm = _normalized_entry(file_entries.get(model.name))
        if model.name not in file_entries:
            entry["source"] = "user_added"
        elif file_norm is not None and file_norm == _normalized_entry(raw):
            entry["source"] = "shipped"
        else:
            entry["source"] = "user_modified"
        snapshot.append(entry)
    return snapshot


def _default_state(app_config: AppConfig) -> tuple[str | None, str | None]:
    """解析默认模型；失败时返回可读原因，使设置页能解释「为什么现在起不来」。"""
    try:
        return app_config.resolve_default_model_name(), None
    except ValueError as exc:
        return None, str(exc)


def settings_snapshot(app_config: AppConfig, config_name: str = CONFIG_FILE_NAME) -> dict:
    """设置页所需的完整快照。"""
    default_name, default_error = _default_state(app_config)
    curated = [model.name for model in app_config.models if model.curation_default]
    file_entries, _ = _raw_layers(config_name)
    return {
        "models": catalog_snapshot(app_config, config_name),
        "shipped_models": [
            _entries_for_submission(entry)
            for entry in (_normalized_entry(raw) for raw in file_entries.values())
            if entry is not None
        ],
        "editable_fields": list(_EDITABLE_FIELDS),
        "removed_models": list(app_config.removed_models),
        "adapters": [dict(adapter) for adapter in SUPPORTED_ADAPTERS],
        "curation_output_methods": list(_CURATION_OUTPUT_METHODS),
        "default_model_name": default_name,
        "default_error": default_error,
        "curation_default_model_name": curated[0] if curated else None,
        "config_file": str(Path(config_name)),
    }


def _entries_for_submission(entry: dict) -> dict:
    """把条目收敛为提交用的字段集合（与读模型共用同一份可编辑字段清单）。"""
    return {field: entry.get(field) for field in _EDITABLE_FIELDS}


def _entry_errors(index: int, entry: Any) -> dict[str, str]:
    """单条目的字段级校验；供目录校验与连通性测试共用。"""
    errors: dict[str, str] = {}
    prefix = f"models[{index}]"
    if not isinstance(entry, dict):
        return {prefix: "条目必须是映射"}

    for key in (key for key in entry if key not in _EDITABLE_FIELDS):
        errors[f"{prefix}.{key}"] = "不支持的字段"

    for field in _REQUIRED_STRING_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            errors[f"{prefix}.{field}"] = "必填"

    supported = {adapter["use"] for adapter in SUPPORTED_ADAPTERS}
    if entry.get("use") and entry["use"] not in supported:
        errors[f"{prefix}.use"] = "适配器不受支持，请从可选列表中选择"

    base_url = entry.get("base_url")
    if isinstance(base_url, str) and base_url and not _is_http_url(base_url):
        errors[f"{prefix}.base_url"] = "必须是 http 或 https 的绝对地址"

    window = entry.get("context_window")
    if window is not None and (not isinstance(window, int) or isinstance(window, bool) or window <= 0):
        errors[f"{prefix}.context_window"] = "必须是正整数"

    tokens = entry.get("curation_max_output_tokens")
    if tokens is not None and (
        not isinstance(tokens, int) or isinstance(tokens, bool) or not 512 <= tokens <= 65536
    ):
        errors[f"{prefix}.curation_max_output_tokens"] = "必须在 512 到 65536 之间"

    method = entry.get("curation_output_method")
    if method is not None and method not in _CURATION_OUTPUT_METHODS:
        errors[f"{prefix}.curation_output_method"] = "取值必须是 " + " / ".join(_CURATION_OUTPUT_METHODS)
    if entry.get("curation_default") and method is None:
        errors[f"{prefix}.curation_output_method"] = "策展默认条目必须声明策展输出方式"

    for flag in ("default", "curation_default", "supports_image_input"):
        if flag in entry and not isinstance(entry[flag], bool):
            errors[f"{prefix}.{flag}"] = "必须是布尔值"

    return errors


def validate_entry(entry: Any) -> None:
    """单条目校验；失败抛 ModelSettingsError。"""
    errors = _entry_errors(0, entry)
    if errors:
        raise ModelSettingsError(errors)


def validate_catalog(models: Any) -> None:
    """写入前目录级校验；失败抛 ModelSettingsError，错误按字段定位。"""
    if not isinstance(models, list):
        raise ModelSettingsError({"models": "模型目录必须是列表"})
    if not models:
        raise ModelSettingsError({"models": "至少需要保留一个模型条目"})

    errors: dict[str, str] = {}
    seen: set[str] = set()
    defaults = 0
    curated = 0

    for index, entry in enumerate(models):
        errors.update(_entry_errors(index, entry))
        name = _entry_name(entry)
        if name is not None:
            if name in seen:
                errors[f"models[{index}].name"] = f"条目名重复: {name}"
            seen.add(name)
        if isinstance(entry, dict):
            defaults += 1 if entry.get("default") else 0
            curated += 1 if entry.get("curation_default") else 0

    if defaults != 1:
        errors["default"] = "必须恰好指定一个默认模型"
    if curated != 1:
        errors["curation_default"] = "必须恰好指定一个策展默认模型"

    if errors:
        raise ModelSettingsError(errors)


def _is_http_url(value: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _validate_credentials(models: Sequence[dict], pending: dict[str, str]) -> None:
    """每个条目的凭据来源变量必须能用：来自本次提交的值，或当前进程内已有值。"""
    errors: dict[str, str] = {}
    for index, entry in enumerate(models):
        variable = credential_variable(entry)
        if variable is None:
            continue  # 字面量密钥由结构校验保证非空
        if pending.get(variable) or credential_is_set(variable):
            continue
        errors[f"models[{index}].api_key"] = f"凭据来源变量未设置: {variable}"
    if errors:
        raise ModelSettingsError(errors)


def _pending_credentials(payload: dict) -> dict[str, str]:
    raw = payload.get("credentials") or {}
    if not isinstance(raw, dict):
        raise ModelSettingsError({"credentials": "凭据必须是变量名到值的映射"})
    return {
        variable: value
        for variable, value in raw.items()
        if isinstance(variable, str) and isinstance(value, str) and value
    }


def _with_inherited_api_keys(submitted: Any, declared: dict[str, dict]) -> Any:
    """提交里留空 api_key 表示不修改该条目的密钥来源，从当前原始声明继承（可能是字面量）。"""
    if not isinstance(submitted, list):
        return submitted
    result = []
    for entry in submitted:
        if isinstance(entry, dict) and not entry.get("api_key"):
            inherited = (declared.get(_entry_name(entry) or "") or {}).get("api_key")
            if isinstance(inherited, str) and inherited:
                entry = {**entry, "api_key": inherited}
        result.append(entry)
    return result


def _with_pending_credentials(models: Sequence[dict], pending: dict[str, str]) -> list[dict]:
    """把本次提交的凭据值就地代入条目，供「落盘前校验」使用（不写入任何文件）。"""
    result = []
    for entry in models:
        variable = credential_variable(entry)
        if variable and pending.get(variable):
            result.append({**entry, "api_key": pending[variable]})
        else:
            result.append(entry)
    return result


def _self_check(config: AppConfig) -> None:
    """装配默认模型，使适配器与凭据问题在写入之前暴露（与启动期自检同一路径）。"""
    from focus.models import create_chat_model

    name = next((model.name for model in config.models if model.default), None)
    if name is None:
        raise ModelSettingsError({"default": "必须恰好指定一个默认模型"})
    try:
        create_chat_model(name=name, app_config=config)
    except ValueError as exc:
        raise ModelSettingsError({"models": str(exc)}) from exc


def save_model_settings(
    app_config: AppConfig,
    payload: dict,
    config_name: str = CONFIG_FILE_NAME,
) -> dict:
    """校验、落盘并就地生效；任何校验失败都发生在写入之前。"""
    file_map, _ = load_layered_maps(config_name, str(Path(config_name)))
    declared = _by_name(merge_model_layers(file_map, {}).get("models") or [])

    submitted = _with_inherited_api_keys(payload.get("models"), declared)
    validate_catalog(submitted)
    pending = _pending_credentials(payload)

    models = [ModelConfig.model_validate(entry).model_dump() for entry in submitted]
    _validate_credentials(models, pending)

    requested_removals = [
        name for name in (payload.get("removed_models") or []) if isinstance(name, str)
    ]
    shipped_names = set(_by_name(file_map.get("models") or []))
    overrides, derived_removed = derive_preference_overlay(models, list(file_map.get("models") or []))
    # 删除名单以服务端推导为准：只有文件层里真实存在的名字才需要被显式记住
    removed = list(
        dict.fromkeys([*derived_removed, *[n for n in requested_removals if n in shipped_names]])
    )
    preference_map = {"models": overrides, "removed_models": removed}

    # 先在内存里构造并自检（待提交凭据就地代入）：失败时磁盘与当前生效配置都不变
    fresh = build_app_config(
        file_map,
        {"models": _with_pending_credentials(overrides, pending), "removed_models": removed},
    )
    _self_check(fresh)

    for variable, value in pending.items():
        apply_credential(variable, value)

    try:
        write_models_preference(overrides, removed, config_name)
    except PreferenceWriteError as exc:
        raise ModelSettingsError({"file": str(exc)}) from exc

    apply_app_config(app_config, fresh)
    logger.info("模型设置已保存并立即生效: %d 个条目", len(fresh.models))
    return settings_snapshot(app_config, config_name)


async def referencing_models(session_factory, names: Sequence[str]) -> dict[str, list[dict]]:
    """按条目名查询引用它的任务、运行、巡检与小兵草稿。"""
    from sqlalchemy import select

    from backend.app.desktop.models import (
        MAIN_RUN_EQUIPMENT_KEY,
        DesktopRun,
        DesktopThread,
        PatrolAgent,
        PatrolDraft,
    )

    wanted = [name for name in names if isinstance(name, str) and name]
    if not wanted:
        return {}

    references: dict[str, list[dict]] = {}
    thread_model = DesktopThread.ui_state[MAIN_RUN_EQUIPMENT_KEY]["model_name"].as_string()

    async with session_factory() as session:
        runs = (await session.execute(
            select(
                DesktopRun.model_name,
                DesktopRun.run_id,
                DesktopRun.task_id,
                DesktopRun.agent_id,
                DesktopRun.kind,
                DesktopRun.status,
            ).where(DesktopRun.model_name.in_(wanted))
        )).all()
        for model_name, run_id, task_id, agent_id, kind, status in runs:
            references.setdefault(model_name, []).append({
                "kind": "run",
                "task_id": task_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "run_kind": kind,
                "status": status,
            })

        threads = (await session.execute(
            select(DesktopThread.task_id, DesktopThread.title, thread_model)
            .where(thread_model.in_(wanted), DesktopThread.deleted_at.is_(None))
        )).all()
        for task_id, title, model_name in threads:
            references.setdefault(model_name, []).append({
                "kind": "task",
                "task_id": task_id,
                "title": title,
            })

        agents = (await session.execute(
            select(
                PatrolAgent.agent_id,
                PatrolAgent.task_id,
                PatrolAgent.equipment["model_name"].as_string(),
            ).where(PatrolAgent.equipment["model_name"].as_string().in_(wanted))
        )).all()
        for agent_id, task_id, model_name in agents:
            references.setdefault(model_name, []).append({
                "kind": "patrol_agent",
                "task_id": task_id,
                "agent_id": agent_id,
            })

        drafts = (await session.execute(
            select(
                PatrolDraft.draft_id,
                PatrolDraft.task_id,
                PatrolDraft.equipment["model_name"].as_string(),
            ).where(PatrolDraft.equipment["model_name"].as_string().in_(wanted))
        )).all()
        for draft_id, task_id, model_name in drafts:
            references.setdefault(model_name, []).append({
                "kind": "patrol_draft",
                "task_id": task_id,
                "draft_id": draft_id,
            })

    return references


async def probe_model_connection(entry: dict, api_key_value: str | None = None) -> dict:
    """用候选条目发起一次最小真实调用；只在人显式点击测试时执行。

    待提交的凭据值只就地代入本次内存配置，不写进程环境、不落盘；返回值与错误信息都做脱敏。
    """
    secrets = [api_key_value] if api_key_value else []
    candidate = {field: entry.get(field) for field in _EDITABLE_FIELDS}
    if api_key_value:
        candidate["api_key"] = api_key_value
    if not candidate.get("api_key"):
        raise ModelSettingsError(
            {"models[0].api_key": "测试连接需要密钥：请输入密钥，或先保存后再测试"}
        )
    validate_entry(candidate)

    from focus.models import create_chat_model

    fresh = build_app_config({"models": [candidate]}, {})

    try:
        model = create_chat_model(
            name=candidate["name"], app_config=fresh, max_tokens=1, temperature=0
        )
        await model.ainvoke("ping")
    except Exception as exc:  # 网络与供应商错误统一在此映射成可读原因
        return {"ok": False, "reason": _redact(_describe_failure(exc), secrets)}
    return {"ok": True, "reason": "连接成功"}


def _describe_failure(exc: Exception) -> str:
    """把供应商/网络异常转成可读原因，不泄漏请求细节。"""
    try:
        import openai
    except ImportError:  # pragma: no cover - openai 是 langchain-openai 的依赖
        return f"{type(exc).__name__}: 连接失败"

    if isinstance(exc, openai.AuthenticationError):
        return "凭据被拒绝（401），请检查密钥"
    if isinstance(exc, openai.PermissionDeniedError):
        return "凭据无权访问该模型（403）"
    if isinstance(exc, openai.NotFoundError):
        return "模型标识或端点路径不存在（404）"
    if isinstance(exc, openai.RateLimitError):
        return "请求被限流（429），请稍后重试"
    if isinstance(exc, openai.APITimeoutError):
        return "请求超时，端点无响应"
    if isinstance(exc, openai.APIConnectionError):
        return "端点不可达，请检查地址与网络"
    if isinstance(exc, openai.APIStatusError):
        return f"端点返回 {exc.status_code}"
    return f"{type(exc).__name__}: 连接失败"
