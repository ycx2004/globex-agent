"""服务器本机账号管理：口令从标准输入读取，不经命令行参数，避免进入 shell 历史与进程列表。

    python -m scripts.accounts --db data/accounts.db set ycx < 口令文件
    python -m scripts.accounts --db data/accounts.db list
    python -m scripts.accounts --db data/accounts.db delete ycx
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from app.infrastructure.account_store import AccountStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("set").add_argument("buyer_id")
    sub.add_parser("delete").add_argument("buyer_id")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    store = AccountStore(args.db)
    if args.command == "set":
        password = getpass.getpass("口令：") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        store.set_password(args.buyer_id, password)
        print("已设置账号口令：", args.buyer_id)
    elif args.command == "delete":
        print("已删除" if store.delete(args.buyer_id) else "账号不存在")
    else:
        print("\n".join(store.list_buyers()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
