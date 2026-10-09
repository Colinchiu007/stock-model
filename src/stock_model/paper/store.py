"""模拟盘账户持久化

为什么需要
----------
``web/paper_api.py`` 的引擎只存在进程内存的 ``_engines`` 字典里，
进程一重启持仓、成交记录、资金曲线**全部丢失**。

这对「手动点一下跑几轮」无所谓，但**定时跑**的场景是致命的 ——
机器重启/发版/崩溃一次，就可能丢掉账户状态甚至重复交易。

本模块负责把 ``Account`` 状态落盘并在下次启动时恢复。

落盘时机
--------
- 每次 ``step()`` / ``run()`` 推进之后
- 手动 ``reset`` 时
- 优雅关闭进程时（可选）

写入采用「先写临时文件再原子替换」，避免写入中途崩溃产生半截文件。

⚠️ **只有账户状态是不够的**：``Account`` 里没有「推进到哪一天」，
那在引擎的 ``_cursor`` 上。恢复时必须同时把 ``_metadata`` 里的游标还给引擎，
否则重启后会从数据区间开头重新推进，在交易过的日期上再交易一遍。
见 ``paper/engine.py`` 的 ``state_dict()`` / ``load_state()``。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from stock_model.paper.models import Account

# 默认存储目录: <项目根>/data/paper/
DEFAULT_STORE_DIR = Path(__file__).resolve().parents[3] / "data" / "paper"


def store_path(account_id: str, store_dir: Path | None = None) -> Path:
    """账户状态文件路径"""
    base = store_dir or DEFAULT_STORE_DIR
    return base / f"{account_id}.json"


def write_json_atomic(path: Path, payload: Any) -> None:
    """原子写入 JSON 文件

    先写**同目录**临时文件(同目录才能保证 ``os.replace`` 是同一文件系统内的
    原子操作), 再替换目标文件。写到一半崩溃时, 目标文件要么是旧内容、
    要么是新内容, 不会出现半截 JSON。

    Raises:
        OSError: 写入或替换失败(临时文件会被清理)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_account(
    account: Account,
    store_dir: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """原子写入账户状态

    Args:
        account: 账户对象
        store_dir: 存储目录，None 用默认
        metadata: 附加信息（如股票池、取数区间、推进游标）

    Returns:
        写入的文件路径
    """
    path = store_path(account.account_id, store_dir)

    payload = account.to_dict()
    if metadata:
        payload["_metadata"] = metadata

    try:
        write_json_atomic(path, payload)
    except OSError as e:
        logger.warning(f"保存账户状态失败 {path}: {e}")
        raise

    logger.info(f"账户状态已保存: {path} (总资产 {account.total_asset:.2f})")
    return path


def load_account(
    account_id: str,
    store_dir: Path | None = None,
) -> Account | None:
    """读取账户状态

    Returns:
        Account 实例；文件不存在或损坏时返回 None
    """
    path = store_path(account_id, store_dir)
    if not path.exists():
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"读取账户状态失败 {path}: {e}")
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"账户状态文件损坏 {path}: {e}")
        return None

    data.pop("_metadata", None)
    try:
        account = Account.from_dict(data)
    except (TypeError, KeyError, ValueError) as e:
        logger.error(f"账户状态反序列化失败 {path}: {e}")
        return None

    logger.info(
        f"账户状态已恢复: {path} "
        f"(总资产 {account.total_asset:.2f}, 持仓 {len(account.positions)}, "
        f"成交 {len(account.trades)})"
    )
    return account


def load_metadata(
    account_id: str,
    store_dir: Path | None = None,
) -> dict[str, Any]:
    """读取账户状态文件里的 ``_metadata``

    为什么需要单独读
    ----------------
    ``_metadata`` 存的是**账户之外**的运行时配置(股票池 / 取数区间 / 推进游标)。
    恢复引擎时必须先拿到它们 —— ``Account`` 上并没有 ``symbols`` / ``data_source``
    / ``start_date`` 这些属性(踩过: 照直觉写 ``account.symbols`` 直接
    ``AttributeError``), 只能从这里取。

    文件不存在 / 损坏 / 字段缺失或类型不对时一律返回 ``{}``, 由调用方回退到默认配置。
    **不抛异常**: 恢复路径上的一次坏文件不该让账户彻底打不开。

    Returns:
        元数据字典; 无法取得时为空字典
    """
    path = store_path(account_id, store_dir)
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"读取账户元数据失败 {path}: {e}")
        return {}

    if not isinstance(data, dict):
        logger.warning(f"账户状态文件结构异常({type(data).__name__}), 无法读取元数据: {path}")
        return {}

    meta = data.get("_metadata")
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        logger.warning(f"账户元数据格式异常({type(meta).__name__}), 已忽略: {path}")
        return {}
    return meta


def delete_account(account_id: str, store_dir: Path | None = None) -> bool:
    """删除账户状态文件"""
    path = store_path(account_id, store_dir)
    if not path.exists():
        return False
    path.unlink()
    logger.info(f"账户状态已删除: {path}")
    return True


def list_accounts(store_dir: Path | None = None) -> list[str]:
    """列出所有已保存的账户 id"""
    base = store_dir or DEFAULT_STORE_DIR
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.json"))
