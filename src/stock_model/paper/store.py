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


def save_account(
    account: Account,
    store_dir: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """原子写入账户状态

    Args:
        account: 账户对象
        store_dir: 存储目录，None 用默认
        metadata: 附加信息（如股票池、策略名）

    Returns:
        写入的文件路径
    """
    path = store_path(account.account_id, store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = account.to_dict()
    if metadata:
        payload["_metadata"] = metadata
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    # 原子替换: 先写同目录临时文件, 再 os.replace 覆盖
    # 避免写到一半崩溃导致状态文件损坏
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        Path(tmp).unlink(missing_ok=True)
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
