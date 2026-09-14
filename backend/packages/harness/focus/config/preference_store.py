"""
本文件对外提供用户偏好层（`~/.focus/config.yaml` 与 `~/.focus/.env`）的读写能力。

对外提供:
    PreferenceWriteError — 无法安全读写用户偏好层时抛出（拒绝写入，绝不覆写）
    preference_config_path(config_name) — 用户偏好层配置文件路径
    preference_env_path() — 用户偏好层凭据文件路径
    read_preference_config() — 以磁盘内容为基线读取往返文档
    write_models_preference(models, removed_models, config_name) — 只改模型目录相关键并原子写出
    credential_is_set(variable) — 该凭据变量在当前进程内是否已设置（只回答是/否，不给出值）
    apply_credential(variable, value) — 写入凭据文件并在当前进程内立即可用

输入:
    models: list[dict] — 用户偏好层要声明的条目（完整声明，不含半条）
    removed_models: list[str] — 显式删除名单
    variable / value: str — 凭据来源变量名与其新值

输出:
    路径、往返文档或 None；写入类函数只以「是否完成」表达结果，MUST NOT 返回凭据值

具体工作流:
    (1) 往返式读取（typ='rt'）保留注释、键顺序与其它配置段落，只替换模型目录相关的键。
    (2) 以临时文件加 `os.replace` 原子写出：中断只会留下旧文件或新文件，不会留下半截文件。
    (3) 解析失败、多文档、顶层不是映射、无法往返时抛 PreferenceWriteError 并保持文件原样。
    (4) 凭据按键替换或追加到 `~/.focus/.env`，其余行原样保留；随后更新当前进程的 os.environ，
        使 require_env_var 立刻解析到新值（load_global_dotenv 只在启动期注入一次且 override=False，
        不刷新就会出现「设置里改了密钥但没生效」）。

为什么不复用 PyYAML：`yaml.safe_dump` 会静默删除用户手写的注释与字段顺序，等于每次保存都
破坏一次用户文件。这里用 ruamel.yaml 的往返模式，这是「程序读写人维护的 YAML」的标准做法。
依赖按需导入：harness 的其它路径不需要它，缺失时给出可执行的提示而不是启动期 ImportError。

示例:
    write_models_preference([entry], ["legacy-model"])
    apply_credential("OPENAI_API_KEY", "sk-...")
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from focus.config.layered import global_home
from focus.config.model_layers import MODELS_KEY, REMOVED_MODELS_KEY

_ENV_FILE_NAME = ".env"
_MISSING_DEPENDENCY_HINT = (
    "缺少 ruamel.yaml 依赖：请重新运行安装器（scripts/focus.ps1 / scripts/focus.sh）"
    "或手动执行 pip install ruamel.yaml 后重启应用"
)


class PreferenceWriteError(RuntimeError):
    """用户偏好层无法安全读写时抛出：宁可拒绝写入，也不覆写用户文件。"""


def preference_config_path(config_name: str = "config.yaml") -> Path:
    """用户偏好层配置文件路径（`~/.focus/<config_name>`）。"""
    return global_home() / config_name


def preference_env_path() -> Path:
    """用户偏好层凭据文件路径（`~/.focus/.env`）。"""
    return global_home() / _ENV_FILE_NAME


def _yaml():
    """构造往返模式 YAML 实例；依赖缺失时给出可执行的提示。"""
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:  # pragma: no cover - 只在缺依赖的安装上触发
        raise PreferenceWriteError(_MISSING_DEPENDENCY_HINT) from exc

    yaml = YAML()  # 默认 typ='rt'：往返保留注释、格式与键顺序
    yaml.preserve_quotes = True
    yaml.width = 4096  # 长 URL 等标量不得被折行
    yaml.indent(mapping=2, sequence=4, offset=2)  # 与发行自带 config.yaml 的书写风格一致
    return yaml


def read_preference_config(config_name: str = "config.yaml"):
    """以磁盘当前内容为基线读取用户偏好层；文件缺失或只有空白时返回空映射。

    多文档、非映射顶层或无法解析时抛 PreferenceWriteError——这类文件不允许被程序改写。
    """
    from ruamel.yaml.comments import CommentedMap

    path = preference_config_path(config_name)
    if not path.is_file():
        return CommentedMap()

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreferenceWriteError(f"无法读取用户偏好层配置: {path}") from exc

    if not text.strip():
        return CommentedMap()

    yaml = _yaml()
    try:
        documents = list(yaml.load_all(io.StringIO(text)))
    except Exception as exc:  # ruamel 的解析异常族较杂，统一转成可读错误
        raise PreferenceWriteError(f"用户偏好层配置无法解析，已拒绝写入: {path}") from exc

    if len(documents) != 1:
        raise PreferenceWriteError(
            f"用户偏好层配置包含 {len(documents)} 个 YAML 文档，无法安全读写: {path}"
        )

    document = documents[0]
    if document is None:
        return CommentedMap()
    if not isinstance(document, dict):
        raise PreferenceWriteError(f"用户偏好层配置顶层不是映射，已拒绝写入: {path}")
    return document


def write_models_preference(
    models: list[dict],
    removed_models: list[str],
    config_name: str = "config.yaml",
) -> Path:
    """把模型目录相关的键写入用户偏好层，其余内容原样保留。返回写入路径。

    只改 `models` 与 `removed_models` 两个键；其余键、注释与键顺序沿用磁盘当前内容，
    因此运行期间的手工编辑不会被丢弃。
    """
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    document = read_preference_config(config_name)

    catalog = CommentedSeq()
    for entry in models:
        item = CommentedMap()
        for key, value in entry.items():
            item[key] = value
        catalog.append(item)
    document[MODELS_KEY] = catalog

    removed = CommentedSeq()
    for name in removed_models:
        removed.append(name)
    document[REMOVED_MODELS_KEY] = removed

    return _dump_atomically(document, preference_config_path(config_name))


def credential_is_set(variable: str) -> bool:
    """该凭据变量在当前进程内是否已设置。只回答是/否，MUST NOT 暴露值。"""
    return bool(os.environ.get(variable))


def apply_credential(variable: str, value: str) -> None:
    """把凭据写入 `~/.focus/.env` 并在当前进程内立即可用。返回值与日志都不含凭据值。"""
    name = (variable or "").strip()
    if not name:
        raise PreferenceWriteError("凭据来源变量名为空")
    if not value:
        return
    if "\n" in value or "\r" in value:
        raise PreferenceWriteError(f"凭据值不能包含换行: {name}")

    _write_env_value(preference_env_path(), name, value)
    # 当前进程内立即生效：require_env_var 在使用期读 os.environ
    os.environ[name] = value


def _write_env_value(path: Path, name: str, value: str) -> None:
    """按键替换或追加 `KEY=value`，其余行（含注释与其它键）原样保留。"""
    lines: list[str] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise PreferenceWriteError(f"无法读取凭据文件: {path}") from exc

    rendered = f"{name}={_quote_env_value(value)}"
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        candidate = stripped[7:] if stripped.startswith("export ") else stripped
        if candidate.split("=", 1)[0].strip() == name and "=" in candidate:
            lines[index] = rendered
            replaced = True
            break
    if not replaced:
        lines.append(rendered)

    _write_text_atomically(path, "\n".join(lines) + "\n")


def _quote_env_value(value: str) -> str:
    """含空白、引号、井号或美元号的值加双引号，其余保持裸值。"""
    if any(char in value for char in ' \t"\'#$'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _dump_atomically(document, path: Path) -> Path:
    stream = io.StringIO()
    _yaml().dump(document, stream)
    _write_text_atomically(path, stream.getvalue())
    return path


def _write_text_atomically(path: Path, text: str) -> None:
    """临时文件 + 原子替换写入；失败时清理临时文件并保持原文件不变。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreferenceWriteError(f"无法创建用户偏好层目录: {path.parent}") from exc

    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PreferenceWriteError(f"写入用户偏好层失败: {path}") from exc
