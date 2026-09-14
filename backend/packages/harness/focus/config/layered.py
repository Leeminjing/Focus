"""
本文件对外提供两层配置聚合层，定义全局态 `~/.focus` 家目录与「用户偏好优先、文件层兜底」的配置合并。

对外提供:
    global_home() — 全局态 `.focus` 家目录 (Path)
    load_global_dotenv() — 启动期加载 `~/.focus/.env` 全局密钥（仅补缺，不覆盖已设置值）
    load_layered_maps(config_name, file_path) — 读取 文件层 + 用户偏好层 两份原始 map
    load_layered_map(config_name, file_path) — 两层深合并为 dict（用户偏好层优先）
    layered_candidate_paths(config_name, file_path) — 该配置的物理候选路径（文件层 + 用户偏好层）
    layered_mtime(config_name, file_path) — 候选文件的最大 mtime（用于 MCP 缓存刷新判定）
    deep_merge(base, overlay) — 深合并两个 dict（overlay 优先）

输入:
    config_name: str — 配置文件基名（如 "config.yaml" / "extensions_config.json"）
    file_path: str — 文件层路径（cwd 相对或绝对）。安装版下它是发行自带的配置，开发检出下
        它是该检出自带的配置；两者都是「这份程序自带的默认」，都不是用户偏好。

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


def _read_yaml_map(path: Path) -> dict:
    """读取 YAML/JSON 配置为 dict；文件不存在或非 dict 时返回空 dict。"""
    import json

    if not path.is_file():
        return {}
    try:
        if path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            import yaml

            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
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


def load_layered_map(config_name: str, file_path: str) -> dict:
    """合并 文件层 + 用户偏好层；用户偏好层优先（作为 overlay）。返回合并后的 dict。"""
    file_map, preference_map = load_layered_maps(config_name, file_path)
    return deep_merge(file_map, preference_map)


def layered_candidate_paths(config_name: str, file_path: str) -> tuple[Path, ...]:
    """报出该配置的全部物理候选路径（文件层、用户偏好层各一），不区分是否存在。"""
    return (Path(file_path), global_home() / config_name)


def layered_mtime(config_name: str, file_path: str) -> float | None:
    """返回文件层/用户偏好层候选文件的最大 mtime；皆不存在返回 None。"""
    candidates = [Path(file_path), global_home() / config_name]
    times = [path.stat().st_mtime for path in candidates if path.is_file()]
    return max(times) if times else None
