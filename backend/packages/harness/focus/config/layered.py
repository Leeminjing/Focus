"""
本文件对外提供两层配置聚合层，定义全局态 `~/.focus` 家目录与「仓库态优先、全局态兜底」的配置合并。

对外提供:
    global_home() — 全局态 `.focus` 家目录 (Path)
    load_global_dotenv() — 启动期加载 `~/.focus/.env` 全局密钥（仅补缺，不覆盖已设置值）
    load_layered_map(config_name, repo_path) — 读取并合并 全局态 + 仓库态 同名配置为 dict（仓库态优先）
    layered_mtime(config_name, repo_path) — 全局态/仓库态候选文件的最大 mtime（用于 MCP 缓存刷新判定）

输入:
    config_name: str — 配置文件基名（如 "config.yaml" / "extensions_config.json"）
    repo_path: str — 仓库态文件路径（cwd 相对或绝对）

输出:
    global_home → Path；load_global_dotenv → None；load_layered_map → dict；layered_mtime → float | None

具体工作流:
    (1) global_home 解析用户主目录下 `.focus`（可用环境变量 FOCUS_GLOBAL_HOME 覆盖，便于测试）。
    (2) load_layered_map 先读全局态 `~/.focus/<config_name>`（缺失则空 dict），再读仓库态 <repo_path>
        （缺失则空 dict），以仓库态为 overlay 深合并（仓库态优先），返回合并后的 dict。
    (3) layered_mtime 取全局态与仓库态候选文件中存在者的最大 mtime；皆不存在返回 None。
    (4) load_global_dotenv 在 os.environ 中仅补缺地注入 `~/.focus/.env` 键值。

示例:
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


def _deep_merge(base: dict, overlay: dict) -> dict:
    """深合并两个 dict：overlay（仓库态/仓库覆盖）优先；dict 递归合并，非 dict 由 overlay 替换。"""
    out = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


def load_layered_map(config_name: str, repo_path: str) -> dict:
    """合并 全局态 + 仓库态 同名配置；仓库态优先（作为 overlay）。返回合并后的 dict。"""
    global_map = _read_yaml_map(global_home() / config_name)
    repo_map = _read_yaml_map(Path(repo_path))
    return _deep_merge(global_map, repo_map)


def layered_mtime(config_name: str, repo_path: str) -> float | None:
    """返回全局态/仓库态候选文件的最大 mtime；皆不存在返回 None。"""
    candidates = [global_home() / config_name, Path(repo_path)]
    times = [path.stat().st_mtime for path in candidates if path.is_file()]
    return max(times) if times else None
