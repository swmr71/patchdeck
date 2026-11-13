"""各ホストへ SSH してパッケージ/サービス/Docker の状態を集める。"""
from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field

import asyncssh

from . import parsers
from .config import settings
from .db import Container, Host, Package, Service, SessionLocal, utcnow
from .jobs import HostResult, JobContext, host_lock, semaphore
from .ssh import connect, run

APT_LOCK = "-o DPkg::Lock::Timeout=120"


@dataclass
class HostInfo:
    id: int
    vmid: int
    name: str
    ip: str
    port: int = 22


@dataclass
class ScanResult:
    os_name: str | None = None
    pkg_manager: str | None = None
    has_docker: bool = False
    reboot_required: bool = False
    packages: list[dict] = field(default_factory=list)
    services: list[dict] = field(default_factory=list)
    containers: list[dict] | None = None   # None = Docker 情報は更新しない
    warnings: list[str] = field(default_factory=list)


def load_hosts(host_ids: list[int] | None = None) -> list[HostInfo]:
    with SessionLocal() as s:
        q = s.query(Host).filter(Host.enabled.is_(True))
        if host_ids:
            q = q.filter(Host.id.in_(host_ids))
        return [HostInfo(h.id, h.vmid, h.name, h.ip, h.port) for h in q.order_by(Host.vmid).all()]


async def _scan_apt(conn, res: ScanResult, ctx: JobContext | None, name: str) -> None:
    r = await run(conn, f'out=$(apt-get {APT_LOCK} update -qq 2>&1); rc=$?; printf "%s\\n" "$out" | tail -n 15; exit $rc',
                  timeout=600)
    if not r.ok:
        msg = f"apt-get update が終了コード {r.exit_status}: {r.stdout.strip()[-300:]}"
        res.warnings.append(msg)
        if ctx:
            ctx.log(msg, name)

    r = await run(conn, "apt list --upgradable 2>/dev/null")
    upgradable = {u.name: u for u in parsers.parse_apt_upgradable(r.stdout)}

    r = await run(conn, r"dpkg-query -W -f='${Package}\t${Version}\t${db:Status-Abbrev}\n'")
    installed = parsers.parse_dpkg_installed(r.stdout)

    rows: dict[str, dict] = {}
    for u in upgradable.values():
        g = settings.group_of(u.name)
        rows[u.name] = dict(name=u.name, installed=u.installed, candidate=u.candidate, suite=u.suite,
                            upgradable=True, security=u.security, group=g.name if g else None)
    # 監視対象(Apache等)は更新が無くても現在のバージョンを記録する
    for pkg, ver in installed.items():
        if pkg in rows:
            continue
        g = settings.group_of(pkg)
        if g:
            rows[pkg] = dict(name=pkg, installed=ver, candidate=None, suite=None,
                             upgradable=False, security=False, group=g.name)
    res.packages = list(rows.values())

    # サービス状態
    units: dict[str, str] = {}
    for g in settings.watch:
        if not g.service:
            continue
        for pkg in installed:
            if g.matches(pkg):
                unit = pkg if g.service == "$pkg" else g.service
                units.setdefault(unit, g.name)
                if g.service != "$pkg":
                    break
    if units:
        names = list(units)
        r = await run(conn, "systemctl is-active " + " ".join(shlex.quote(u) for u in names), timeout=60)
        states = parsers.parse_systemctl_is_active(names, r.stdout)
        res.services = [dict(unit=u, state=states[u], group=units[u]) for u in names]


async def _scan_docker(conn, res: ScanResult, ctx: JobContext | None, name: str) -> None:
    r = await run(conn, "ids=$(docker ps -q); if [ -n \"$ids\" ]; then docker inspect $ids; else echo '[]'; fi", timeout=120)
    if not r.ok:
        res.warnings.append(f"docker inspect 失敗: {r.stderr.strip()[:200]}")
        return
    containers = parsers.parse_docker_inspect(r.stdout)

    latest: dict[str, tuple[str | None, str | None]] = {}   # image -> (latest_id, note)
    for img in sorted({c.image for c in containers}):
        if "@sha256:" in img or img.startswith("sha256:"):
            latest[img] = (None, "ダイジェスト固定のため確認対象外")
            continue
        note = None
        if settings.docker_check_pull:
            if ctx:
                ctx.log(f"docker pull {img} で新しいイメージを確認中", name)
            p = await run(conn, f"docker pull -q {shlex.quote(img)}", timeout=900)
            if not p.ok:
                note = "pull 失敗(ローカルビルド or レジストリ到達不可?): " + (p.stderr or p.stdout).strip()[:200]
        i = await run(conn, f"docker image inspect -f '{{{{.Id}}}}' {shlex.quote(img)}", timeout=60)
        latest[img] = (i.stdout.strip() if i.ok else None, note)

    rows = []
    for c in containers:
        lid, note = latest.get(c.image, (None, None))
        upd = None
        if lid and not note:
            upd = lid != c.image_id
        if not c.compose_project:
            extra = "docker run で作られたコンテナのため自動更新は非対応"
            note = f"{note} / {extra}" if note else extra
        rows.append(dict(name=c.name, image=c.image, state=c.state, image_id=c.image_id,
                         latest_image_id=lid, update_available=upd,
                         compose_project=c.compose_project, compose_service=c.compose_service,
                         compose_dir=c.compose_dir, compose_files=c.compose_files, note=note))
    res.containers = rows


async def scan_unlocked(host: HostInfo, ctx: JobContext | None) -> HostResult:
    """ロック取得済み前提のスキャン本体。"""
    if ctx:
        ctx.log(f"スキャン開始 ({host.ip})", host.name)
    res = ScanResult()
    try:
        async with await connect(host.ip, host.port) as conn:
            r = await run(conn, "cat /etc/os-release 2>/dev/null; echo; "
                                "command -v apt-get >/dev/null && echo __HAS_APT__; "
                                "command -v docker >/dev/null && echo __HAS_DOCKER__; "
                                "test -f /var/run/reboot-required && echo __REBOOT__; true", timeout=60)
            info = parsers.parse_os_release(r.stdout)
            res.os_name = info.get("PRETTY_NAME") or info.get("NAME")
            res.has_docker = "__HAS_DOCKER__" in r.stdout
            res.reboot_required = "__REBOOT__" in r.stdout
            if "__HAS_APT__" in r.stdout:
                res.pkg_manager = "apt"
                await _scan_apt(conn, res, ctx, host.name)
            else:
                res.pkg_manager = None
                res.warnings.append("apt 以外のディストリは未対応")
            if res.has_docker:
                await _scan_docker(conn, res, ctx, host.name)
            else:
                res.containers = []
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
        msg = f"接続/実行エラー: {type(e).__name__}: {e}"
        await asyncio.to_thread(_save_error, host.id, msg)
        if ctx:
            ctx.log(msg, host.name)
        return HostResult(host.name, False, msg)

    await asyncio.to_thread(_save_result, host.id, res)
    upg = sum(1 for p in res.packages if p["upgradable"])
    sec = sum(1 for p in res.packages if p["security"])
    dock = sum(1 for c in (res.containers or []) if c["update_available"])
    msg = f"更新 {upg} 件 (セキュリティ {sec})" + (f" / Docker 更新 {dock}" if res.has_docker else "")
    if res.warnings:
        msg += " ⚠ " + " / ".join(res.warnings)
    if ctx:
        ctx.log("スキャン完了: " + msg, host.name)
    return HostResult(host.name, True, msg)


async def scan_host(host: HostInfo, ctx: JobContext | None) -> HostResult:
    async with host_lock(host.id), semaphore():
        return await scan_unlocked(host, ctx)


async def scan_many(hosts: list[HostInfo], ctx: JobContext) -> list[HostResult]:
    return list(await asyncio.gather(*(scan_host(h, ctx) for h in hosts)))


def _save_error(host_id: int, msg: str) -> None:
    with SessionLocal() as s:
        h = s.get(Host, host_id)
        h.last_scan_at = utcnow()
        h.last_scan_ok = False
        h.last_error = msg
        s.commit()


def _save_result(host_id: int, res: ScanResult) -> None:
    with SessionLocal() as s:
        h = s.get(Host, host_id)
        h.os_name = res.os_name
        h.pkg_manager = res.pkg_manager
        h.has_docker = res.has_docker
        h.reboot_required = res.reboot_required
        h.last_scan_at = utcnow()
        h.last_scan_ok = True
        h.last_error = " / ".join(res.warnings) or None
        h.packages.clear()
        h.services.clear()
        s.flush()
        h.packages.extend(Package(**p) for p in res.packages)
        h.services.extend(Service(**sv) for sv in res.services)
        if res.containers is not None:
            h.containers.clear()
            s.flush()
            h.containers.extend(Container(**c) for c in res.containers)
        s.commit()
