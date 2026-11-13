"""更新処理(パッケージ単位 / ホスト丸ごと / Docker コンテナ)。"""
from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass

import asyncssh

from .db import Container, Host, SessionLocal
from .jobs import HostResult, JobContext, host_lock, semaphore
from .scanner import HostInfo, scan_unlocked
from .ssh import connect, run_stream

APT = ("apt-get -o DPkg::Lock::Timeout=120 "
       "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold")

# dpkg のパッケージ名(+ :arch)として妥当なものだけ通す
_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*(:[a-z0-9]+)?$")


def valid_pkg(name: str) -> bool:
    return bool(_PKG_RE.match(name))


async def _exec(host: HostInfo, ctx: JobContext, cmd: str, label: str) -> HostResult:
    """ホストでコマンドを流し、終わったら再スキャンして DB を最新にする。"""
    async with host_lock(host.id), semaphore():
        ctx.log(f"{label} 開始", host.name)
        ctx.log(f"$ {cmd}", host.name)
        tail: list[str] = []

        def on_line(line: str) -> None:
            ctx.log(line, host.name)
            tail.append(line)
            if len(tail) > 5:
                tail.pop(0)

        try:
            async with await connect(host.ip, host.port) as conn:
                code = await run_stream(conn, cmd, on_line)
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
            msg = f"接続/実行エラー: {type(e).__name__}: {e}"
            ctx.log(msg, host.name)
            return HostResult(host.name, False, msg)

        if code == 0:
            ctx.log(f"{label} 完了", host.name)
            result = HostResult(host.name, True, f"{label} 完了")
        else:
            ctx.log(f"{label} 失敗 (終了コード {code})", host.name)
            last = tail[-1] if tail else ""
            result = HostResult(host.name, False, f"{label} 失敗 (終了コード {code}) {last[:150]}")

        ctx.log("更新後の状態を再スキャン", host.name)
        scan = await scan_unlocked(host, ctx)
        if result.ok and scan.ok:
            result.message += f" → {scan.message}"
        return result


async def update_packages(host: HostInfo, packages: list[str], ctx: JobContext) -> HostResult:
    pk = [p for p in packages if valid_pkg(p)]
    if not pk:
        return HostResult(host.name, False, "更新対象パッケージがありません")
    cmd = (f"{APT} update -qq && "
           f"{APT} install --only-upgrade -y " + " ".join(shlex.quote(p) for p in pk))
    return await _exec(host, ctx, cmd, f"パッケージ更新 ({', '.join(pk)})")


async def full_upgrade(host: HostInfo, ctx: JobContext) -> HostResult:
    cmd = f"{APT} update -qq && {APT} upgrade -y"
    return await _exec(host, ctx, cmd, "apt upgrade")


@dataclass
class ContainerInfo:
    id: int
    name: str
    image: str
    compose_project: str | None
    compose_service: str | None
    compose_dir: str | None
    compose_files: str | None


def compose_command(c: ContainerInfo) -> str | None:
    if not (c.compose_project and c.compose_service):
        return None
    files = [f for f in (c.compose_files or "").split(",") if f.strip()]
    fargs = " ".join(f"-f {shlex.quote(f.strip())}" for f in files)
    base = f'$DC -p {shlex.quote(c.compose_project)} {fargs}'.strip()
    svc = shlex.quote(c.compose_service)
    cd = f"cd {shlex.quote(c.compose_dir)} && " if c.compose_dir else ""
    return ('if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC=docker-compose; fi; '
            f"{cd}{base} pull {svc} && {base} up -d {svc}")


async def update_containers(host: HostInfo, containers: list[ContainerInfo], ctx: JobContext) -> HostResult:
    cmds = []
    skipped = []
    for c in containers:
        cmd = compose_command(c)
        if cmd:
            cmds.append(f"echo '>>> {c.name}'; ( {cmd} )")
        else:
            skipped.append(c.name)
    if skipped:
        ctx.log("compose 管理外のためスキップ: " + ", ".join(skipped), host.name)
    if not cmds:
        return HostResult(host.name, False, "更新できるコンテナがありません(compose 管理外)")
    # 1つ失敗しても残りは続け、最後に失敗があれば非0で終える
    script = "rc=0; " + " ".join(f"{c} || rc=1;" for c in cmds) + " exit $rc"
    names = ", ".join(c.name for c in containers if c.name not in skipped)
    return await _exec(host, ctx, script, f"Docker 更新 ({names})")


def load_containers(ids: list[int]) -> dict[int, tuple[HostInfo, list[ContainerInfo]]]:
    """コンテナID → ホストごとにまとめる。"""
    out: dict[int, tuple[HostInfo, list[ContainerInfo]]] = {}
    with SessionLocal() as s:
        for c in s.query(Container).filter(Container.id.in_(ids)).all():
            h: Host = c.host
            if not h.enabled:
                continue
            hi = HostInfo(h.id, h.vmid, h.name, h.ip, h.port)
            out.setdefault(h.id, (hi, []))[1].append(ContainerInfo(
                c.id, c.name, c.image, c.compose_project, c.compose_service, c.compose_dir, c.compose_files))
    return out
