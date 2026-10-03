"""config.toml 的读写层：网页改配置直接写插件自己的 config.toml（2026-10 改造）。

- config.toml 是唯一真实来源（数据库不再存配置覆盖层）。
- tomlkit 读-改-写：注释和格式保留；每次写前重新读文件（宿主/用户可能手改过）。
- 关键风险（docs/06）：插件目录里 config.toml 之外新建/改文件 = 源码变化 → 全部插件
  热重载。所以绝不在插件目录建临时文件：内存里改好，open(path, "w") 直接覆盖写。
- 写前把旧内容备份到数据目录 <data_dir>/config-backups/config.toml-<时间戳>（只留
  最近 10 份，0600）；config.toml 写完保持 0600。
- 同进程一把锁串行化写；跨进程冲突（宿主同时在写）靠「写前重读 + 改完整体覆盖」兜，
  冲突窗口极小，且宿主随后会发同样内容的 on_config_update，幂等。
- 类型映射：groups.serve / environments.ssh → [[节.字段]] 表数组；console.listen →
  "IP:端口" 字符串；list → 数组；其余标量。删除键 = 回到代码默认值（reset）。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import tomlkit
from tomlkit.items import AoT, Table

logger = logging.getLogger("maiwork.config_file")

CONFIG_FILENAME = "config.toml"
_BACKUP_DIRNAME = "config-backups"
_BACKUP_KEEP = 10

_LOCK = threading.Lock()

# 「表数组」形态的字段（节.字段 → 每条目的键集合）
_AOT_FIELDS: dict[str, tuple[str, ...]] = {
    "groups.serve": ("group", "workspace"),
    "environments.ssh": ("name", "host", "note"),
}

# 顶层「整段就是表数组」的两节（2026-10 模型改版）：只能整段重写（增删改都在内存里
# 算好整段再写），键顺序固定；空串/空列表的键不写进文件（读回时按默认补全）。
_AOT_TOP_FIELDS: dict[str, tuple[str, ...]] = {
    "endpoints": ("id", "name", "protocol", "base_url", "api_key", "retries", "retry_delay_s", "max_concurrency", "max_rpm", "headers"),
    "model_list": ("id", "endpoint", "model", "name", "efforts", "vision", "context_window", "max_tokens"),
}


class ConfigFileError(Exception):
    """config.toml 读 / 写失败；message 是中文，给网页 400/500 用。"""


def config_path(plugin_dir: Path | str) -> Path:
    return Path(plugin_dir) / CONFIG_FILENAME


def read_text(plugin_dir: Path | str) -> str:
    """读 config.toml 原文；文件不存在抛 ConfigFileError。"""
    path = config_path(plugin_dir)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigFileError(f"找不到 {CONFIG_FILENAME}（{path}）；插件目录里没有配置文件") from None
    except OSError as e:
        raise ConfigFileError(f"读 {CONFIG_FILENAME} 失败：{e}") from None


# ----------------------------------------------------------------------
# 备份
# ----------------------------------------------------------------------


def _backup_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / _BACKUP_DIRNAME


def _write_backup(data_dir: Path | str, old_text: str) -> Path:
    """把旧内容备份到数据目录；返回备份文件路径。目录建不了/写不了都抛 ConfigFileError。"""
    bdir = _backup_dir(data_dir)
    try:
        bdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigFileError(f"建配置备份目录失败（{bdir}）：{e}") from None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = bdir / f"{CONFIG_FILENAME}-{stamp}-{int(time.time() * 1000) % 1_000_000:06d}"
    n = 1
    while path.exists():  # 同一毫秒内连写两次：别互相覆盖（机器忙时实测会撞名）
        path = bdir / f"{CONFIG_FILENAME}-{stamp}-{int(time.time() * 1000) % 1_000_000:06d}-{n}"
        n += 1
    try:
        path.write_text(old_text, encoding="utf-8")
        os.chmod(path, stat.S_IMODE(0o600))
    except OSError as e:
        raise ConfigFileError(f"写配置备份失败（{path}）：{e}") from None
    _prune_backups(bdir)
    return path


def _prune_backups(bdir: Path) -> None:
    """只留最近 _BACKUP_KEEP 份；清理失败只记日志，不拖垮写。"""
    try:
        files = sorted(bdir.glob(f"{CONFIG_FILENAME}-*"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - _BACKUP_KEEP)]:
            try:
                old.unlink()
            except OSError:
                logger.warning("清旧配置备份失败：%s", old)
    except OSError:
        logger.warning("列配置备份目录失败：%s", bdir)


# ----------------------------------------------------------------------
# 改文档（内存中）
# ----------------------------------------------------------------------


def _section(doc: Any, name: str, *, create: bool) -> Any:
    """拿到节表；没有时 create=True 则新建一个空表追加到文档末尾。"""
    sec = doc.get(name)
    if sec is None:
        if not create:
            return None
        sec = tomlkit.table()
        doc[name] = sec
    return sec


def _set_value(sec: Any, key: str, value: Any) -> None:
    """往节里写值；标量 / 字符串 / 布尔 / 数字 / 数组。值为 None 表示删除该键。"""
    if value is None:
        if key in sec:
            del sec[key]
        return
    if isinstance(value, bool):
        sec[key] = value
    elif isinstance(value, (int, float)):
        sec[key] = value
    elif isinstance(value, str):
        sec[key] = value
    elif isinstance(value, (list, tuple)):
        arr = tomlkit.array()
        for item in value:
            arr.append(item)
        sec[key] = arr
    else:
        sec[key] = str(value)


def _set_aot(sec: Any, key: str, entries: Iterable[dict], order: tuple[str, ...]) -> None:
    """写 [[节.键]] 表数组：整组替换。空列表 = 写空数组（明确「一个都没有」）。

    条目里空字符串的键不写（groups.serve 的 workspace 空 = 用默认 "g<群号>"，
    不写行——宿主规范化时一样处理）。
    """
    items = [dict(e) for e in entries]
    existing = sec.get(key)
    # 已有一个数组/表数组：整组替换（先删再建，位置不变）
    if existing is not None:
        del sec[key]
    aot = tomlkit.aot()
    for item in items:
        t = tomlkit.table()
        for k in order:
            v = item.get(k)
            if v is None or (isinstance(v, str) and not v):
                continue
            t[k] = v
        aot.append(t)
    sec[key] = aot


def _apply(doc: Any, flat: dict[str, Any]) -> list[str]:
    """把 {"节.字段": 值} 应用到 tomlkit 文档；返回实际改动的「节.字段」清单（排序）。

    值 None = 删除键（回默认）。
    """
    changed: list[str] = []
    for full_key, value in flat.items():
        section, _, field = str(full_key).partition(".")
        if not section or not field:
            raise ConfigFileError(f'配置键要写成 "节.字段"，收到 "{full_key}"')
        # 表数组字段
        if full_key in _AOT_FIELDS:
            sec = _section(doc, section, create=True)
            if value is None:
                if field in sec:
                    del sec[field]
                    changed.append(full_key)
                continue
            if not isinstance(value, (list, tuple)):
                raise ConfigFileError(f"{full_key} 要填一个列表")
            _set_aot(sec, field, value, _AOT_FIELDS[full_key])
            changed.append(full_key)
            continue
        # 普通字段
        sec = _section(doc, section, create=value is not None)
        if sec is None:
            continue  # 节不存在且要删键 = 已经是默认，无事发生
        before = tomlkit.dumps(sec)
        _set_value(sec, field, value)
        if tomlkit.dumps(sec) != before:
            changed.append(full_key)
    return sorted(set(changed))


def _apply_deletions(doc: Any, keys: Iterable[str]) -> list[str]:
    deleted: list[str] = []
    for full_key in keys:
        section, _, field = str(full_key).partition(".")
        sec = doc.get(section)
        if sec is None or field not in sec:
            continue
        del sec[field]
        deleted.append(str(full_key))
    return sorted(set(deleted))


# ----------------------------------------------------------------------
# 写文件
# ----------------------------------------------------------------------


def _write_back(plugin_dir: Path | str, data_dir: Path | str, text: str, *, old_text: str) -> None:
    """备份旧内容 → 直接覆盖写 config.toml（0600）。写失败抛 ConfigFileError。

    绝不在插件目录建临时文件（会触发宿主全部插件热重载，docs/06）。
    """
    _write_backup(data_dir, old_text)
    path = config_path(plugin_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, stat.S_IMODE(0o600))
    except OSError as e:
        raise ConfigFileError(f"写 {CONFIG_FILENAME} 失败（{path}）：{e}") from None


def write_aot_section(plugin_dir: Path | str, data_dir: Path | str, section: str, entries: Iterable[dict]) -> str:
    """整段重写 [[endpoints]] / [[model_list]]（2026-10 模型改版）。

    - section 必须在 _AOT_TOP_FIELDS 里登记（键顺序固定）；
    - 每条目空字符串 / 空列表的键不写（读回时按默认补；api_key 空串自然不写）；
    - 空列表 → tomlkit 空 aot 落不进文本，等于这节从文件里去掉（一个都没有）；
    - 和 write_fields 同一条备份+0600 链路；返回写完后的文件全文。
    """
    if section not in _AOT_TOP_FIELDS:
        raise ConfigFileError(f"{section} 不是登记过的「整段表数组」节（{_AOT_TOP_FIELDS.keys()}）")
    order = _AOT_TOP_FIELDS[section]
    items = [dict(e) for e in entries]
    aot = tomlkit.aot()
    for item in items:
        t = tomlkit.table()
        for k in order:
            v = item.get(k)
            if v is None:
                continue
            if isinstance(v, str) and not v:
                continue
            if isinstance(v, (list, tuple)) and not v:
                continue
            t[k] = list(v) if isinstance(v, (list, tuple)) else v
        aot.append(t)
    with _LOCK:
        old_text = read_text(plugin_dir)
        try:
            doc = tomlkit.parse(old_text)
        except Exception as e:
            raise ConfigFileError(f"{CONFIG_FILENAME} 解析失败（文件坏了？）：{e}") from None
        if aot:
            doc[section] = aot
        else:
            # 空列表：tomlkit 空 aot dumps 不进文本，等于这节从文件去掉（一个都没有）
            if doc.get(section) is not None:
                del doc[section]
        new_text = tomlkit.dumps(doc)
        _write_back(plugin_dir, data_dir, new_text, old_text=old_text)
        return new_text


def write_fields(plugin_dir: Path | str, data_dir: Path | str, flat: dict[str, Any]) -> str:
    """改字段（值 None = 删键回默认）：重读文件 → 内存里改 → 备份 → 覆盖写。

    返回写完后的文件全文（给「写后立即应用」的调用方用，不用再读一次）。
    一个字段的写法不合法（不是「节.字段」）→ 整个不落盘（ConfigFileError）。
    """
    if not flat:
        return read_text(plugin_dir)
    with _LOCK:
        old_text = read_text(plugin_dir)
        try:
            doc = tomlkit.parse(old_text)
        except Exception as e:
            raise ConfigFileError(f"{CONFIG_FILENAME} 解析失败（文件坏了？）：{e}") from None
        _apply(doc, flat)
        new_text = tomlkit.dumps(doc)
        _write_back(plugin_dir, data_dir, new_text, old_text=old_text)
        return new_text


def delete_fields(plugin_dir: Path | str, data_dir: Path | str, keys: Iterable[str]) -> str:
    """删键（回代码默认值）；键不存在幂等。返回写完后的文件全文。"""
    keys = [str(k) for k in keys]
    with _LOCK:
        old_text = read_text(plugin_dir)
        try:
            doc = tomlkit.parse(old_text)
        except Exception as e:
            raise ConfigFileError(f"{CONFIG_FILENAME} 解析失败（文件坏了？）：{e}") from None
        _apply_deletions(doc, keys)
        new_text = tomlkit.dumps(doc)
        _write_back(plugin_dir, data_dir, new_text, old_text=old_text)
        return new_text
