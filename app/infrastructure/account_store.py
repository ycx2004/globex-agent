"""本机账号口令库：scrypt 加盐哈希，只由服务器命令行创建账号；页面不提供注册。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.infrastructure.identity import buyer_identity

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
 buyer_id TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL)
"""
# scrypt 参数随哈希一同固定；调整时需要按版本迁移，不能静默替换已有口令。
_N, _R, _P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 12
# 连续失败锁定：按账号计数，避免经反向代理后同一来源 IP 无法区分。
MAX_FAILURES, LOCK_SECONDS = 8, 900


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)


class AccountLocked(RuntimeError):
    pass


class AccountStore:
    def __init__(self, path: str | Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._failures: dict[str, tuple[int, float]] = {}
        self._guard = threading.Lock()
        with self._db() as db:
            db.execute(SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def set_password(self, buyer_id: str, password: str) -> None:
        buyer = buyer_identity(buyer_id)
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH or len(password) > 256:
            raise ValueError(f"口令长度需为 {MIN_PASSWORD_LENGTH} 至 256 个字符")
        salt = secrets.token_bytes(16)
        now = self.clock()
        with self._db() as db:
            db.execute("INSERT INTO accounts(buyer_id,salt,password_hash,created_at,updated_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(buyer_id) DO UPDATE SET salt=excluded.salt,password_hash=excluded.password_hash,updated_at=excluded.updated_at",
                       (buyer, salt, _hash(password, salt), now, now))
        with self._guard:
            self._failures.pop(buyer, None)

    def delete(self, buyer_id: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM accounts WHERE buyer_id=?", (buyer_identity(buyer_id),)).rowcount == 1

    def list_buyers(self) -> list[str]:
        with self._db() as db:
            return [row[0] for row in db.execute("SELECT buyer_id FROM accounts ORDER BY buyer_id")]

    def verify(self, buyer_id: str, password: str) -> bool:
        """账号不存在时也做一次同成本哈希，避免用耗时差异探测账号。"""
        try:
            buyer = buyer_identity(buyer_id)
        except ValueError:
            return False
        now = self.clock()
        with self._guard:
            count, since = self._failures.get(buyer, (0, now))
            if count >= MAX_FAILURES and now - since < LOCK_SECONDS:
                raise AccountLocked("连续登录失败次数过多，请稍后再试")
            if now - since >= LOCK_SECONDS:
                self._failures.pop(buyer, None)
        with self._db() as db:
            row = db.execute("SELECT salt,password_hash FROM accounts WHERE buyer_id=?", (buyer,)).fetchone()
        salt, expected = row if row else (b"\0" * 16, b"\0" * 32)
        ok = isinstance(password, str) and 0 < len(password) <= 256 and hmac.compare_digest(_hash(password, salt), expected) and row is not None
        with self._guard:
            if ok:
                self._failures.pop(buyer, None)
            else:
                count, since = self._failures.get(buyer, (0, now))
                self._failures[buyer] = (count + 1, since if count else now)
        return ok
