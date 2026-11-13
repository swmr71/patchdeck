"""asyncssh の薄いラッパー。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import asyncssh

from .config import settings

# すべてのリモートコマンドの頭に付ける(出力を英語に固定して解析しやすくする)
ENV_PREFIX = "export LANG=C LC_ALL=C DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a; "


@dataclass
class CmdResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


async def connect(ip: str, port: int = 22) -> asyncssh.SSHClientConnection:
    kwargs: dict = dict(
        port=port,
        username=settings.ssh_user,
        client_keys=[settings.ssh_key_path],
        connect_timeout=settings.ssh_connect_timeout,
        keepalive_interval=30,
    )
    # known_hosts=None はホスト鍵検証なし。SSH_KNOWN_HOSTS を空にした場合のみ
    kwargs["known_hosts"] = settings.ssh_known_hosts if settings.ssh_known_hosts else None
    return await asyncssh.connect(ip, **kwargs)


async def run(conn: asyncssh.SSHClientConnection, cmd: str, timeout: int | None = None) -> CmdResult:
    r = await asyncio.wait_for(conn.run(ENV_PREFIX + cmd, check=False), timeout or settings.cmd_timeout)
    status = r.exit_status if r.exit_status is not None else -1
    return CmdResult(status, str(r.stdout or ""), str(r.stderr or ""))


async def run_stream(
    conn: asyncssh.SSHClientConnection,
    cmd: str,
    on_line: Callable[[str], Awaitable[None] | None],
    timeout: int | None = None,
) -> int:
    """標準出力+標準エラーを1行ずつ on_line に流しながら実行し、終了コードを返す。"""

    async def _inner() -> int:
        async with conn.create_process(ENV_PREFIX + cmd, stderr=asyncssh.STDOUT) as proc:
            async for line in proc.stdout:
                res = on_line(line.rstrip("\n"))
                if asyncio.iscoroutine(res):
                    await res
            await proc.wait()
            return proc.exit_status if proc.exit_status is not None else -1

    return await asyncio.wait_for(_inner(), timeout or settings.cmd_timeout)
