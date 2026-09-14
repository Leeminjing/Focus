"""
本文件对外提供 merge_model_layers 纯函数，定义 config.yaml 中模型目录的两层合并语义。

对外提供:
    merge_model_layers(file_map, preference_map) — 以文件层为默认、用户偏好层为覆盖，返回合并后的顶层 map
    SINGLETON_FLAGS — 跨条目的单例标记名（default / curation_default）
    REMOVED_MODELS_KEY — 用户偏好层表达「删除某条目」的顶层键名

输入:
    file_map: dict — 文件层（`<cwd>/config.yaml`）原始 map，即这份程序自带的默认
    preference_map: dict — 用户偏好层（`~/.focus/config.yaml`）原始 map，由设置界面写入

输出:
    dict — 合并后的顶层 map；一般键按 deep_merge 由用户偏好层覆盖，models 按下述语义合并

具体工作流:
    (1) 一般键：用户偏好层优先（deep_merge），与该层作为 overlay 的整体语义一致。
    (2) models：以条目 name 为单位逐条合并——用户偏好层声明的条目覆盖同名文件层条目，
        文件层中未被声明的条目保留，用户偏好层新增的条目并入（追加在末尾）。
        条目位置沿用文件层顺序，使界面顺序不因用户改动而跳动。
    (3) removed_models：用户偏好层的显式删除名单，合并后按 name 剔除同名条目。
        名单中指向不存在条目的项被忽略（升级删掉某条目后，旧名单不应导致加载失败）。
    (4) default / curation_default：跨条目的单例标记。用户偏好层只要有任一条目声明了某个标记，
        文件层其它条目的同名标记就被清除——否则合并结果会出现两个默认，加载期直接失败。
    (5) 两层都未声明 models 时，输出不注入 models 键，让「未声明模型目录」在加载期照旧显式失败。

为什么不是「用户层声明 models 即全集」：那会让用户第一次保存后，发行层后续新增的模型再也不
出现，只能靠「恢复发行默认」补救；也会把「改一个字段」变成「维护整份目录」。

为什么删除要用显式名单：条目名被持久化在任务与运行的 equipment 中，删除必须是一次可审计的
声明，且 MUST NOT 在文件层更新后被静默复活。

示例:
    merged = merge_model_layers({"models": [{"name": "a"}]}, {"models": [{"name": "a", "default": True}]})
"""

from focus.config.layered import deep_merge

SINGLETON_FLAGS = ("default", "curation_default")
"""跨条目的单例标记：至多一个条目为真，因此只能由声明它的最高层独占。"""

REMOVED_MODELS_KEY = "removed_models"
"""用户偏好层表达「删除某条目」的顶层键名：值是被删除条目名的列表。"""

MODELS_KEY = "models"


def merge_model_layers(file_map: dict, preference_map: dict) -> dict:
    """合并两层 config.yaml 原始 map，按条目名合并 models 并落实删除名单与单例标记归属。"""
    merged = deep_merge(file_map, preference_map)

    declared = MODELS_KEY in file_map or MODELS_KEY in preference_map
    if declared:
        removed = _removed_names(merged.get(REMOVED_MODELS_KEY))
        merged[MODELS_KEY] = _merge_catalogs(
            _entry_list(file_map.get(MODELS_KEY)),
            _entry_list(preference_map.get(MODELS_KEY)),
            removed,
        )
        merged[REMOVED_MODELS_KEY] = removed
    return merged


def _normalized(entry: object) -> dict | None:
    """把一份原始条目收敛为声明式完整条目（补上缺省字段）；无法校验时返回 None。"""
    from pydantic import ValidationError

    from focus.config.model_config import ModelConfig

    if not isinstance(entry, dict):
        return None
    try:
        return ModelConfig.model_validate(entry).model_dump()
    except ValidationError:
        return None


def derive_preference_overlay(
    effective_entries: list, file_entries: list
) -> tuple[list, list[str]]:
    """由「期望的生效目录」与文件层目录推导用户偏好层应当写入的内容。

    返回 (用户条目, 删除名单)。规则与 merge_model_layers 互为逆运算：

    - 与文件层同名的条目只在内容不同时写入（内容相同就不落盘，避免把发行默认复制成用户副本）；
    - 文件层中没有对应名字的条目直接写入（用户新增）；
    - 文件层里存在、但期望目录里没有的条目名进入删除名单（用户删除）。

    比较一律在「规范化后的完整条目」上进行：文件层通常省略 default / curation_default /
    supports_image_input / curation_max_output_tokens 这些缺省字段，直接与完整条目比较会把
    每个条目都误判成「用户改过」，从而把整份发行目录复制进用户偏好层。

    「内容不同即由用户接管」是刻意的：清除默认标记这类改动也必须落到用户层，否则文件层的
    声明会重新生效。
    """
    file_by_name: dict[str, dict] = {}
    for entry in file_entries:
        name = _entry_name(entry)
        if name is not None:
            file_by_name[name] = _normalized(entry) or entry

    effective_names = {
        name for entry in effective_entries if (name := _entry_name(entry)) is not None
    }

    overrides: list = []
    for entry in effective_entries:
        name = _entry_name(entry)
        if name is None or file_by_name.get(name) != _normalized(entry):
            # 无 name 的条目原样保留，交给加载期校验报错
            overrides.append(entry)

    removed = [name for name in file_by_name if name not in effective_names]
    return overrides, removed


def _entry_list(value: object) -> list:
    """把某层的 models 值收敛为列表；缺失或非列表时返回空列表。"""
    return list(value) if isinstance(value, list) else []


def _removed_names(value: object) -> list[str]:
    """把删除名单收敛为去重后的条目名列表；非字符串项与空白项被丢弃。"""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and (name := item.strip()) and name not in names:
            names.append(name)
    return names


def _entry_name(entry: object) -> str | None:
    """取条目的合并键（name）；不是映射或没有字符串 name 时返回 None。"""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    return name if isinstance(name, str) else None


def _claimed_flags(preference_entries: list) -> set[str]:
    """用户偏好层声明了哪些单例标记——这些标记由该层独占。"""
    return {
        flag
        for flag in SINGLETON_FLAGS
        if any(isinstance(entry, dict) and entry.get(flag) for entry in preference_entries)
    }


def _merge_catalogs(base_entries: list, preference_entries: list, removed: list[str]) -> list:
    """按条目名合并两层 models：用户偏好层覆盖同名，未声明者保留，删除名单剔除。"""
    claimed = _claimed_flags(preference_entries)
    merged: list = []
    positions: dict[str, int] = {}

    for entry in base_entries:
        name = _entry_name(entry)
        if name is None:
            # 无 name 或缺 name 的条目原样保留，让加载期校验就它显式报错
            merged.append(entry)
            continue
        stripped = entry
        if claimed:
            stripped = {key: value for key, value in entry.items() if key not in claimed}
        positions[name] = len(merged)
        merged.append(stripped)

    for entry in preference_entries:
        name = _entry_name(entry)
        if name is None:
            merged.append(entry)
            continue
        if name in positions:
            merged[positions[name]] = entry
        else:
            positions[name] = len(merged)
            merged.append(entry)

    removed_set = set(removed)
    return [entry for entry in merged if _entry_name(entry) not in removed_set]
