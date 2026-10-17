"""
本文件提供程序默认与全局偏好的配置聚合，可显式传入工作区增加最高优先级覆盖。

对外提供:
    global_home() — 全局态 `.focus` 家目录 (Path)
    load_global_dotenv() — 启动期加载 `~/.focus/.env` 全局密钥（仅补缺，不覆盖已设置值）
    load_layered_maps(config_name, file_path) — 读取 文件层 + 用户偏好层 两份原始 map
    load_layered_map(config_name, file_path, workspace=...) — 默认 < 全局 < 工作区深合并
    layered_candidate_paths(config_name, file_path, workspace=...) — 实际加载的配置候选路径
    layered_mtime(config_name, file_path) — 兼容旧调用的两层最大 mtime；MCP 缓存使用有效配置摘要
    deep_merge(base, overlay) — 深合并两个 dict（overlay 优先）

输入:
    config_name: str — 配置文件基名（如 "config.yaml" / "extensions_config.json"）
    file_path: str — 文件层路径（cwd 相对或绝对）。安装版下它是发行自带的配置，开发检出下
        它是该检出自带的配置；两者都是「这份程序自带的默认」，都不是用户偏好。
    workspace: str | Path | None — 可选工作区，覆盖来自 <workspace>/.focus/<config_name>

输出:
    global_home → Path；load_global_dotenv → None；load_layered_maps → tuple[dict, dict]；
    load_layered_map → dict；deep_merge → dict；layered_candidate_paths → tuple[Path, ...]；
    layered_mtime → float | None

具体工作流:
    (1) global_home 解析用户主目录下 `.focus`（可用环境变量 FOCUS_GLOBAL_HOME 覆盖，便于测试）。
    (2) load_layered_maps 先读文件层 <file_path>（缺失则空 dict），再读用户偏好层
        `~/.focus/<config_name>`（缺失则空 dict）。
    (3) load_layered_map 以「文件层为 base、用户偏好层为 overlay」深合并（用户偏好优先）。
        这个方向是刻意的：用户偏好是用户自己写下的决定，文件层只是这份程序自带的默认，
        升级会整体替换它，因此 MUST NOT 反过来覆盖用户偏好。
    (4) layered_mtime 取文件层与用户偏好层候选文件中存在者的最大 mtime；皆不存在返回 None。
    (5) load_global_dotenv 在 os.environ 中仅补缺地注入 `~/.focus/.env` 键值。
    (6) layered_candidate_paths 报出该配置的全部物理候选路径（两层各一），不区分是否存在——
        「新建一个配置文件」同样会改变未来的执行，因此权柄面判定的依据是加载器会去看哪里，
        而不是它这次找到了什么。

示例:
    file_map, preference_map = load_layered_maps("config.yaml", "config.yaml")
    cfg = load_layered_map("config.yaml", "config.yaml")
    mt = layered_mtime("extensions_config.json", "extensions_config.json")
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_GLOBAL_DIR_NAME = ".focus"


def global_home() -> Path:
    """返回全局态 `.focus` 家目录；FOCUS_GLOBAL_HOME 环境变量可覆盖（便于测试）。"""
    override = os.environ.get("FOCUS_GLOBAL_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / _GLOBAL_DIR_NAME


def load_global_dotenv() -> None:
    """启动期加载 `~/.focus/.env` 全局密钥；仅补缺（override=False），不覆盖已设置的环境变量。"""
    env_file = global_home() / ".env"
    if env_file.is_file():
        load_dotenv(dotenv_path=str(env_file), override=False)


class LayeredConfigError(ValueError):
    """来源明确的配置文件读取失败；诊断不包含配置正文或凭据。"""

    def __init__(self, path: Path, scope: str, reason: str):
        self.config_path = str(path.resolve())
        self.scope = scope
        self.reason = reason
        self.action = f"修复配置文件 {self.config_path}，然后重试 MCP。"
        super().__init__(f"{scope} 配置文件 {self.config_path}：{reason}")


def _read_yaml_map(path: Path, *, scope: str | None = None) -> dict:
    """缺失文件返回空 dict；指定来源的扩展配置读取遇到损坏时显式失败。"""
    import json

    if scope is None and not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            import yaml

            raw = yaml.safe_load(text) or {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        if scope is not None:
            raise LayeredConfigError(
                path, scope, f"JSON 语法错误（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}",
            ) from exc
        return {}
    except (OSError, ValueError) as exc:
        if scope is not None:
            raise LayeredConfigError(path, scope, f"配置文件无法读取或解码（{type(exc).__name__}）") from exc
        return {}
    if scope is not None and not isinstance(raw, dict):
        raise LayeredConfigError(path, scope, "配置文件顶层必须是 JSON 对象")
    return raw if isinstance(raw, dict) else {}


def deep_merge(base: dict, overlay: dict) -> dict:
    """深合并两个 dict：overlay 优先；dict 递归合并，非 dict 由 overlay 替换。"""
    out = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = deep_merge(current, value)
        else:
            out[key] = value
    return out


def load_layered_maps(config_name: str, file_path: str) -> tuple[dict, dict]:
    """读取两层原始 map，返回 (文件层, 用户偏好层)。

    需要按字段自定义合并语义的调用方（如 config.yaml 的 models）应使用本函数，
    而不是先深合并再拆分。
    """
    file_map = _read_yaml_map(Path(file_path))
    preference_map = _read_yaml_map(global_home() / config_name)
    return file_map, preference_map


def load_layered_map(config_name: str, file_path: str, *, workspace: str | Path | None = None) -> dict:
    """程序默认 < 用户全局 < 工作区 .focus 配置；未传工作区时保持两层行为。"""
    file_map, preference_map = load_layered_maps(config_name, file_path)
    merged = deep_merge(file_map, preference_map)
    if workspace is not None and Path(workspace).resolve() != global_home().resolve():
        merged = deep_merge(merged, _read_yaml_map(Path(workspace) / ".focus" / config_name))
    return merged


def load_layered_map_with_sources(
    config_name: str, file_path: str, *, workspace: str | Path | None = None,
) -> tuple[dict, dict[str, dict[str, dict[str, str]]]]:
    """合并配置并记录条目来源；损坏文件抛出 LayeredConfigError，禁止静默回落。"""
    merged: dict = {}
    sources: dict = {}
    paths = layered_candidate_paths(config_name, file_path, workspace=workspace)
    for scope, path in zip(("default", "global", "workspace"), paths):
        layer = _read_yaml_map(path, scope=scope)
        merged = deep_merge(merged, layer)
        for section, entries in layer.items():
            if isinstance(entries, dict):
                for name in entries:
                    sources.setdefault(section, {})[name] = {
                        "scope": scope, "config_path": str(path.resolve()),
                    }
    return merged, sources


def layered_candidate_paths(
    config_name: str, file_path: str, *, workspace: str | Path | None = None,
) -> tuple[Path, ...]:
    """报出程序默认、全局与可选工作区的候选路径，不区分是否存在。"""
    paths = [Path(file_path), global_home() / config_name]
    if workspace is not None and Path(workspace).resolve() != global_home().resolve():
        paths.append(Path(workspace) / ".focus" / config_name)
    return tuple(paths)


def layered_mtime(config_name: str, file_path: str) -> float | None:
    """返回文件层/用户偏好层候选文件的最大 mtime；皆不存在返回 None。"""
    candidates = [Path(file_path), global_home() / config_name]
    times = [path.stat().st_mtime for path in candidates if path.is_file()]
    return max(times) if times else None
