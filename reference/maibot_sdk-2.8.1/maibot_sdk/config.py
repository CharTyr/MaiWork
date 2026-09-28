"""插件配置模型与 Schema 工具。

该模块为插件系统提供统一的配置声明能力：

1. 插件作者通过 ``PluginConfigBase`` 定义强类型配置模型。
2. Host / Runner 可基于模型生成默认配置字典。
3. WebUI 可基于模型生成插件配置页面需要的 Schema。
"""

# ruff: noqa: I001

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal, TypeVar, cast, get_args, get_origin

import inspect

from pydantic import BaseModel, ConfigDict, Field
from pydantic.fields import FieldInfo

__all__ = [
    "Field",
    "PluginConfigVersionError",
    "PluginConfigBase",
    "build_plugin_default_config",
    "extract_plugin_config_version",
    "generate_plugin_config_schema",
    "is_plugin_config_class",
    "merge_plugin_config_data",
    "rebuild_plugin_config_data",
    "validate_plugin_config",
]

PluginConfigT = TypeVar("PluginConfigT", bound="PluginConfigBase")
_PLUGIN_CONFIG_SECTION_NAME = "plugin"
_PLUGIN_CONFIG_VERSION_FIELD_NAME = "config_version"


class PluginConfigVersionError(ValueError):
    """插件配置版本不合法时抛出的异常。"""


class PluginConfigBase(BaseModel):
    """插件配置模型基类。

    插件作者应通过继承该基类来声明插件配置结构，并为每个字段提供默认值。
    运行时会基于该模型生成默认配置文件、校验配置数据，并构造 WebUI 表单 Schema。
    """

    model_config = ConfigDict(validate_assignment=True, extra="ignore")

    __ui_label__: ClassVar[str] = ""
    """当前配置节在 WebUI 中展示的标题。"""

    __ui_icon__: ClassVar[str] = ""
    """当前配置节在 WebUI 中展示的图标名称。"""

    __ui_order__: ClassVar[int] = 0
    """当前配置节在 WebUI 中的排序序号。"""

    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {}
    """当前配置节在 WebUI 中的多语言展示文本。"""


def is_plugin_config_class(candidate: Any) -> bool:
    """判断给定对象是否为插件配置模型类。

    Args:
        candidate: 待判断的对象。

    Returns:
        bool: 若对象是 ``PluginConfigBase`` 子类，则返回 ``True``。
    """

    return bool(inspect.isclass(candidate) and issubclass(candidate, PluginConfigBase))


def build_plugin_default_config(config_class: type[PluginConfigT]) -> dict[str, Any]:
    """构造插件配置模型的默认配置字典。

    Args:
        config_class: 插件配置模型类。

    Returns:
        Dict[str, Any]: 根据模型默认值导出的配置字典。

    Raises:
        ValueError: 当配置模型存在缺少默认值的字段时抛出。
    """

    try:
        config_instance = config_class()
    except Exception as exc:
        raise ValueError(
            f"插件配置模型 {config_class.__name__} 需要为所有字段提供默认值，当前无法构造默认配置"
        ) from exc
    return config_instance.model_dump(mode="python")


def extract_plugin_config_version(config_data: Mapping[str, Any]) -> str:
    """提取插件配置中的版本号。

    Args:
        config_data: 插件配置字典。

    Returns:
        str: ``plugin.config_version`` 的规范化字符串值。

    Raises:
        PluginConfigVersionError: 当缺少 ``[plugin]`` 配置节或 ``config_version``
            字段为空时抛出。
    """

    plugin_section = config_data.get(_PLUGIN_CONFIG_SECTION_NAME)
    if not isinstance(plugin_section, Mapping):
        raise PluginConfigVersionError("插件配置文件缺少 [plugin] 配置节，且必须提供 plugin.config_version 版本号")

    version_value = plugin_section.get(_PLUGIN_CONFIG_VERSION_FIELD_NAME)
    normalized_version = str(version_value or "").strip()
    if not normalized_version:
        raise PluginConfigVersionError("插件配置文件缺少 plugin.config_version 版本号，当前版本策略不再兼容无版本配置")
    return normalized_version


def merge_plugin_config_data(
    default_config: Mapping[str, Any],
    current_config: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """按默认配置补齐当前配置缺失字段。

    Args:
        default_config: 默认配置内容。
        current_config: 当前已有配置内容。

    Returns:
        Tuple[Dict[str, Any], bool]: 合并后的配置字典，以及是否发生变更。
    """

    merged_config = _deep_copy_mapping(current_config)
    changed = _fill_missing_fields(merged_config, default_config)
    return merged_config, changed


def rebuild_plugin_config_data(
    default_config: Mapping[str, Any],
    current_config: Mapping[str, Any],
) -> dict[str, Any]:
    """基于默认结构重建插件配置。

    该方法用于版本升级场景：以最新默认配置为骨架，仅迁移仍然存在的旧字段值，
    从而达到“补齐新增字段、移除废弃字段、保留用户已有值”的效果。

    Args:
        default_config: 最新默认配置内容。
        current_config: 旧版本配置内容。

    Returns:
        dict[str, Any]: 按最新结构重建后的配置字典。
    """

    rebuilt_config = _deep_copy_mapping(default_config)
    _overlay_existing_fields(rebuilt_config, current_config)
    return rebuilt_config


def validate_plugin_config(config_class: type[PluginConfigT], config_data: Mapping[str, Any]) -> PluginConfigT:
    """使用插件配置模型校验配置数据。

    Args:
        config_class: 插件配置模型类。
        config_data: 待校验的配置数据。

    Returns:
        PluginConfigT: 校验并归一化后的配置模型实例。
    """

    return config_class.model_validate(dict(config_data))


def generate_plugin_config_schema(
    config_class: type[PluginConfigBase],
    *,
    plugin_id: str = "",
    plugin_name: str = "",
    plugin_version: str = "",
    plugin_description: str = "",
    plugin_author: str = "",
) -> dict[str, Any]:
    """根据插件配置模型生成插件配置页面 Schema。

    Args:
        config_class: 插件配置模型类。
        plugin_id: 插件 ID。
        plugin_name: 插件名称。
        plugin_version: 插件版本。
        plugin_description: 插件描述。
        plugin_author: 插件作者。

    Returns:
        Dict[str, Any]: 插件配置页面使用的 Schema 字典。
    """

    default_config = build_plugin_default_config(config_class)
    sections: dict[str, Any] = {}
    general_fields: dict[str, Any] = {}
    general_order = 0

    for field_index, (field_name, field_info) in enumerate(config_class.model_fields.items()):
        field_default = default_config.get(field_name)
        if is_plugin_config_class(field_info.annotation):
            nested_class = cast(type[PluginConfigBase], field_info.annotation)
            section_schema = _build_section_schema(
                section_name=field_name,
                config_class=nested_class,
                current_values=field_default if isinstance(field_default, Mapping) else {},
                fallback_order=field_index,
            )
            sections[field_name] = section_schema
            continue

        general_fields[field_name] = _build_field_schema(
            field_name=field_name,
            field_info=field_info,
            default_value=field_default,
            order=general_order,
        )
        general_order += 1

    if general_fields:
        sections["general"] = {
            "name": "general",
            "title": "通用设置",
            "description": "",
            "icon": "settings",
            "collapsed": False,
            "order": len(sections),
            "fields": general_fields,
        }

    return {
        "plugin_id": plugin_id,
        "plugin_info": {
            "name": plugin_name,
            "version": plugin_version,
            "description": plugin_description,
            "author": plugin_author,
        },
        "sections": sections,
        "layout": {"type": "auto", "tabs": []},
    }


def _build_section_schema(
    *,
    section_name: str,
    config_class: type[PluginConfigBase],
    current_values: Mapping[str, Any],
    fallback_order: int,
) -> dict[str, Any]:
    """构造单个配置节的 Schema。

    Args:
        section_name: 配置节名称。
        config_class: 当前配置节的模型类。
        current_values: 当前配置节默认值。
        fallback_order: 未声明顺序时的兜底排序值。

    Returns:
        Dict[str, Any]: 单个配置节的 Schema。
    """

    field_schemas: dict[str, Any] = {}
    for field_index, (field_name, field_info) in enumerate(config_class.model_fields.items()):
        field_schemas[field_name] = _build_field_schema(
            field_name=field_name,
            field_info=field_info,
            default_value=current_values.get(field_name),
            order=field_index,
        )

    section_title = getattr(config_class, "__ui_label__", "") or section_name
    section_icon = getattr(config_class, "__ui_icon__", "") or None
    section_order = getattr(config_class, "__ui_order__", fallback_order)
    section_i18n = _normalize_i18n(getattr(config_class, "__ui_i18n__", None))
    section_doc = (config_class.__doc__ or "").strip()

    section_schema: dict[str, Any] = {
        "name": section_name,
        "title": section_title,
        "description": section_doc,
        "icon": section_icon,
        "collapsed": False,
        "order": section_order,
        "fields": field_schemas,
    }
    if section_i18n:
        section_schema["i18n"] = section_i18n
    return section_schema


def _build_field_schema(
    *,
    field_name: str,
    field_info: FieldInfo,
    default_value: Any,
    order: int,
) -> dict[str, Any]:
    """构造单个字段的插件配置 Schema。

    Args:
        field_name: 字段名称。
        field_info: Pydantic 字段定义。
        default_value: 字段默认值。
        order: 字段排序序号。

    Returns:
        Dict[str, Any]: 单个字段的 Schema。
    """

    select_choices, is_multiple_select = _extract_select_choices(field_info.annotation)
    field_type = _map_field_type(field_info.annotation)
    json_extra = _normalize_json_schema_extra(field_info)
    ui_type = str(json_extra.get("x-widget") or _default_ui_type(field_type))
    icon_name = _optional_str(json_extra.get("x-icon"))
    if is_multiple_select:
        item_type, item_fields = None, None
    else:
        item_type, item_fields = _extract_list_item_schema(field_info.annotation)
    min_value, max_value = _extract_numeric_constraints(field_info)

    field_schema: dict[str, Any] = {
        "name": field_name,
        "type": field_type,
        "default": default_value,
        "description": field_info.description or "",
        "required": field_info.is_required(),
        "choices": select_choices,
        "min": min_value,
        "max": max_value,
        "step": json_extra.get("step"),
        "pattern": json_extra.get("pattern"),
        "max_length": json_extra.get("max_length"),
        "label": str(json_extra.get("label") or field_name),
        "placeholder": _optional_str(json_extra.get("placeholder")),
        "hint": _optional_str(json_extra.get("hint")),
        "icon": icon_name,
        "hidden": bool(json_extra.get("hidden", False)),
        "disabled": bool(json_extra.get("disabled", False)),
        "order": int(json_extra.get("order", order)),
        "input_type": _optional_str(json_extra.get("input_type")),
        "ui_type": ui_type,
        "rows": int(json_extra.get("rows", 3)),
        "group": _optional_str(json_extra.get("group")),
        "depends_on": _optional_str(json_extra.get("depends_on")),
        "depends_value": json_extra.get("depends_value"),
        "item_type": item_type,
        "item_fields": item_fields,
        "min_items": json_extra.get("min_items"),
        "max_items": json_extra.get("max_items"),
        "example": json_extra.get("example"),
    }
    if is_multiple_select:
        field_schema["multiple"] = True
    if i18n := _normalize_i18n(json_extra.get("i18n")):
        field_schema["i18n"] = i18n
    return field_schema


def _normalize_json_schema_extra(field_info: FieldInfo) -> dict[str, Any]:
    """将字段的 ``json_schema_extra`` 归一化为字典。

    Args:
        field_info: Pydantic 字段定义。

    Returns:
        Dict[str, Any]: 归一化后的扩展元数据字典。
    """

    json_extra = getattr(field_info, "json_schema_extra", None)
    return dict(json_extra) if isinstance(json_extra, dict) else {}


def _normalize_i18n(value: Any) -> dict[str, dict[str, str]] | None:
    """将 WebUI i18n 元数据归一化为可序列化字典。"""

    if not isinstance(value, Mapping):
        return None

    normalized: dict[str, dict[str, str]] = {}
    for locale, locale_entries in value.items():
        if not isinstance(locale_entries, Mapping):
            continue

        entries: dict[str, str] = {}
        for key, text in locale_entries.items():
            if text is None:
                continue
            normalized_text = str(text).strip()
            if normalized_text:
                entries[str(key)] = normalized_text

        if entries:
            normalized[str(locale)] = entries

    return normalized or None


def _extract_numeric_constraints(field_info: FieldInfo) -> tuple[float | None, float | None]:
    """提取字段上的数值范围约束。

    Args:
        field_info: Pydantic 字段定义。

    Returns:
        Tuple[Optional[float], Optional[float]]: ``(最小值, 最大值)``。
    """

    min_value: float | None = None
    max_value: float | None = None
    for metadata in getattr(field_info, "metadata", []):
        if hasattr(metadata, "ge") and metadata.ge is not None:
            min_value = float(metadata.ge)
        if hasattr(metadata, "le") and metadata.le is not None:
            max_value = float(metadata.le)
    return min_value, max_value


def _extract_literal_choices(annotation: Any) -> list[Any] | None:
    """提取 ``Literal`` 字段的可选值列表。

    Args:
        annotation: 字段类型注解。

    Returns:
        Optional[List[Any]]: 可选值列表；若不是 ``Literal`` 则返回 ``None``。
    """

    origin = get_origin(annotation)
    if str(origin) != "typing.Literal":
        return None
    choices = list(get_args(annotation))
    return choices or None


def _extract_select_choices(annotation: Any) -> tuple[list[Any] | None, bool]:
    """提取 select 字段的可选值及是否为多选。"""

    literal_choices = _extract_literal_choices(annotation)
    if literal_choices is not None:
        return literal_choices, False

    origin = get_origin(annotation)
    if origin is not list:
        return None, False

    args = get_args(annotation)
    if not args:
        return None, False

    item_choices = _extract_literal_choices(args[0])
    if item_choices is None:
        return None, False
    return item_choices, True


def _extract_list_item_schema(annotation: Any) -> tuple[str | None, dict[str, dict[str, Any]] | None]:
    """提取列表字段的元素类型描述。

    Args:
        annotation: 字段类型注解。

    Returns:
        Tuple[Optional[str], Optional[Dict[str, Dict[str, Any]]]]: 列表元素类型及对象元素字段定义。
    """

    origin = get_origin(annotation)
    if origin is not list:
        return None, None

    args = get_args(annotation)
    if not args:
        return "string", None

    item_type = args[0]
    if is_plugin_config_class(item_type):
        nested_class = cast(type[PluginConfigBase], item_type)
        item_fields: dict[str, dict[str, Any]] = {}
        default_values = build_plugin_default_config(nested_class)
        for field_name, field_info in nested_class.model_fields.items():
            json_extra = _normalize_json_schema_extra(field_info)
            item_choices, item_multiple = _extract_select_choices(field_info.annotation)
            item_field: dict[str, Any] = {
                "type": _map_field_type(field_info.annotation),
                "label": str(json_extra.get("label") or field_info.description or field_name),
                "placeholder": _optional_str(json_extra.get("placeholder")) or "",
                "default": default_values.get(field_name),
            }
            if item_choices is not None:
                item_field["choices"] = item_choices
            if item_multiple:
                item_field["multiple"] = True
            if i18n := _normalize_i18n(json_extra.get("i18n")):
                item_field["i18n"] = i18n
            item_fields[field_name] = item_field
        return "object", item_fields

    if item_type in {int, float}:
        return "number", None
    if item_type is bool:
        return "boolean", None
    return "string", None


def _map_field_type(annotation: Any) -> str:
    """将 Python 类型注解映射为前端字段类型。

    Args:
        annotation: 字段类型注解。

    Returns:
        str: 前端字段类型字符串。
    """

    origin = get_origin(annotation)

    if origin is list:
        args = get_args(annotation)
        if len(args) == 1 and get_origin(args[0]) is Literal:
            return "select"
        return "array"
    if origin is dict:
        return "object"
    if origin is Literal:
        return "select"
    if is_plugin_config_class(annotation):
        return "object"
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is str:
        return "string"
    return "string"


def _default_ui_type(field_type: str) -> str:
    """根据字段类型推导默认 UI 组件类型。

    Args:
        field_type: 字段类型字符串。

    Returns:
        str: 默认 UI 组件类型。
    """

    if field_type == "boolean":
        return "switch"
    if field_type in {"number", "integer"}:
        return "number"
    if field_type == "array":
        return "list"
    if field_type == "object":
        return "json"
    if field_type == "select":
        return "select"
    return "text"


def _deep_copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """递归复制映射对象。

    Args:
        value: 待复制的映射对象。

    Returns:
        Dict[str, Any]: 深复制后的普通字典。
    """

    copied: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            copied[str(key)] = _deep_copy_mapping(cast(Mapping[str, Any], item))
        elif isinstance(item, list):
            copied[str(key)] = _deep_copy_list(item)
        else:
            copied[str(key)] = item
    return copied


def _deep_copy_list(values: list[Any]) -> list[Any]:
    """递归复制列表对象。

    Args:
        values: 待复制的列表。

    Returns:
        List[Any]: 深复制后的列表。
    """

    copied: list[Any] = []
    for item in values:
        if isinstance(item, Mapping):
            copied.append(_deep_copy_mapping(cast(Mapping[str, Any], item)))
        elif isinstance(item, list):
            copied.append(_deep_copy_list(item))
        else:
            copied.append(item)
    return copied


def _fill_missing_fields(target: dict[str, Any], defaults: Mapping[str, Any]) -> bool:
    """递归向目标配置补齐缺失字段。

    Args:
        target: 当前配置字典。
        defaults: 默认配置字典。

    Returns:
        bool: 是否发生了字段补齐。
    """

    changed = False
    for key, default_value in defaults.items():
        if key not in target:
            target[key] = (
                _deep_copy_mapping(cast(Mapping[str, Any], default_value))
                if isinstance(default_value, Mapping)
                else _deep_copy_list(default_value)
                if isinstance(default_value, list)
                else default_value
            )
            changed = True
            continue

        current_value = target[key]
        if isinstance(current_value, dict) and isinstance(default_value, Mapping):
            if _fill_missing_fields(current_value, cast(Mapping[str, Any], default_value)):
                changed = True

    return changed


def _overlay_existing_fields(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """将旧配置中的已有字段覆盖到新配置骨架中。

    Args:
        target: 以最新默认配置构造出的目标配置字典。
        source: 旧版本配置字典。
    """

    for key, source_value in source.items():
        if key not in target:
            continue
        if key == _PLUGIN_CONFIG_VERSION_FIELD_NAME:
            continue

        target_value = target[key]
        if isinstance(target_value, dict) and isinstance(source_value, Mapping):
            _overlay_existing_fields(target_value, source_value)
            continue

        if isinstance(source_value, Mapping):
            target[key] = _deep_copy_mapping(source_value)
        elif isinstance(source_value, list):
            target[key] = _deep_copy_list(source_value)
        else:
            target[key] = source_value


def _optional_str(value: Any) -> str | None:
    """将任意值安全转换为可选字符串。

    Args:
        value: 待转换的值。

    Returns:
        Optional[str]: 转换后的字符串；空值时返回 ``None``。
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None
