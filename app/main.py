"""PatchDeck — clusters-prj 用のサーバー更新管理ツール。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import notify
from .auth import current_user, require_write
from .config import settings
from .db import Container, Host, Job, Package, SessionLocal, init_db
from .jobs import LIVE, HostResult, JobContext, host_lock, mark_interrupted_jobs, start_job
from .scanner import HostInfo, load_hosts, scan_many
from .updater import full_upgrade, load_containers, update_containers, update_packages, valid_pkg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("patchdeck")
STATIC = Path(__file__).parent / "static"

_state: dict = {"next_scan_at": None, "last_scan_signature": None}


# ---------------------------------------------------------------- 起動処理

def sync_targets() -> None:
    """settings.yaml の targets を DB に反映(無くなったものは無効化)。"""
    with SessionLocal() as s:
        existing = {h.vmid: h for h in s.query(Host).all()}
        seen = set()
        for t in settings.targets:
            seen.add(t.vmid)
            h = existing.get(t.vmid)
            if h is None:
                s.add(Host(vmid=t.vmid, name=t.name, ip=t.ip, port=t.port, enabled=t.enabled))
            else:
                h.name, h.ip, h.port, h.enabled = t.name, t.ip, t.port, t.enabled
        for vmid, h in existing.items():
            if vmid not in seen:
                h.enabled = False
        s.commit()


def _scan_signature() -> tuple[str, str]:
    """定期スキャン通知用: セキュリティ更新とエラーの状況を要約し、変化検知用のハッシュも返す。"""
    lines = []
    with SessionLocal() as s:
        for h in s.query(Host).filter(Host.enabled.is_(True)).order_by(Host.vmid).all():
            if h.last_scan_ok is False:
                lines.append(f"❌ {h.vmid} {h.name}: {h.last_error}")
                continue
            sec = sorted(p.name for p in h.packages if p.security)
            dock = [c.name for c in h.containers if c.update_available]
            parts = []
            if sec:
                parts.append(f"セキュリティ更新 {len(sec)} 件 ({', '.join(sec[:6])}{' …' if len(sec) > 6 else ''})")
            if dock:
                parts.append(f"Docker 更新 {len(dock)} 件 ({', '.join(dock[:4])})")
            if h.reboot_required:
                parts.append("再起動待ち")
            if parts:
                lines.append(f"⚠️ {h.vmid} {h.name}: " + " / ".join(parts))
    text = "\n".join(lines)
    return text, hashlib.sha256(text.encode()).hexdigest()


async def _after_scheduled_scan(ctx: JobContext, status: str, results: list[HostResult]) -> None:
    text, sig = await asyncio.to_thread(_scan_signature)
    if sig == _state["last_scan_signature"]:
        return  # 前回通知から変化なし
    _state["last_scan_signature"] = sig
    if text:
        await notify.send("定期スキャン: 対応が必要なホストがあります", text, "partial")
    else:
        await notify.send("定期スキャン: すべて最新です", "セキュリティ更新・エラーはありません", "success")


async def _after_update(ctx: JobContext, status: str, results: list[HostResult]) -> None:
    await notify.job_finished(ctx.title, status, "\n".join(r.line() for r in results))


async def _scheduler() -> None:
    delay = 30 if settings.scan_on_startup else settings.scan_interval_hours * 3600
    while True:
        _state["next_scan_at"] = datetime.now() + timedelta(seconds=delay)
        await asyncio.sleep(delay)
        delay = settings.scan_interval_hours * 3600
        try:
            hosts = await asyncio.to_thread(load_hosts, None)
            await start_job("scan", f"定期スキャン ({len(hosts)}台)",
                            lambda ctx: scan_many(hosts, ctx), "scheduler", _after_scheduled_scan)
        except Exception:  # noqa: BLE001
            log.exception("定期スキャンの起動に失敗")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_db)
    await asyncio.to_thread(sync_targets)
    await asyncio.to_thread(mark_interrupted_jobs)
    task = None
    if settings.scan_interval_hours > 0:
        task = asyncio.create_task(_scheduler())
    yield
    if task:
        task.cancel()


app = FastAPI(title="PatchDeck", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


# ---------------------------------------------------------------- 画面

@app.get("/", include_in_schema=False)
def index(user: str = Depends(current_user)):
    return FileResponse(STATIC / "index.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- 参照 API

@app.get("/api/overview")
def overview(user: str = Depends(current_user)):
    hosts = []
    with SessionLocal() as s:
        for h in s.query(Host).filter(Host.enabled.is_(True)).order_by(Host.vmid).all():
            hosts.append(dict(
                id=h.id, vmid=h.vmid, name=h.name, ip=h.ip, os_name=h.os_name, pkg_manager=h.pkg_manager,
                has_docker=h.has_docker, reboot_required=h.reboot_required,
                last_scan_at=iso(h.last_scan_at), last_scan_ok=h.last_scan_ok, last_error=h.last_error,
                upgradable=sum(1 for p in h.packages if p.upgradable),
                security=sum(1 for p in h.packages if p.security),
                services=[dict(unit=sv.unit, state=sv.state, group=sv.group) for sv in h.services],
                docker_total=len(h.containers),
                docker_updates=sum(1 for c in h.containers if c.update_available),
                busy=host_lock(h.id).locked(),
            ))
        running = [dict(id=j.id, title=j.title, kind=j.kind)
                   for j in s.query(Job).filter(Job.status.in_(["queued", "running"])).order_by(Job.id).all()]
    nxt = _state["next_scan_at"]
    return dict(hosts=hosts, running_jobs=running, user=user,
                next_scan_at=nxt.isoformat() if nxt else None,
                groups=[g.name for g in settings.watch])


@app.get("/api/hosts/{host_id}")
def host_detail(host_id: int, user: str = Depends(current_user)):
    with SessionLocal() as s:
        h = s.get(Host, host_id)
        if not h:
            raise HTTPException(404)
        pk = sorted(h.packages, key=lambda p: (not p.security, not p.upgradable, p.name))
        return dict(
            id=h.id, vmid=h.vmid, name=h.name, ip=h.ip, os_name=h.os_name, last_error=h.last_error,
            packages=[dict(name=p.name, installed=p.installed, candidate=p.candidate, suite=p.suite,
                           upgradable=p.upgradable, security=p.security, group=p.group) for p in pk],
        )


@app.get("/api/groups")
def groups(user: str = Depends(current_user)):
    """監視対象ソフト(Apache 等)ごとに、どのホストに入っていてバージョンがどうか。"""
    out = []
    with SessionLocal() as s:
        hosts = s.query(Host).filter(Host.enabled.is_(True)).order_by(Host.vmid).all()
        for g in settings.watch:
            rows = []
            for h in hosts:
                pk = [p for p in h.packages if p.group == g.name]
                if not pk:
                    continue
                primary = min(pk, key=lambda p: (g.primary_rank(p.name), p.name))
                upg = [p for p in pk if p.upgradable]
                svc = [dict(unit=sv.unit, state=sv.state) for sv in h.services if sv.group == g.name]
                rows.append(dict(
                    host_id=h.id, vmid=h.vmid, host=h.name, package=primary.name,
                    installed=primary.installed, candidate=primary.candidate,
                    upgradable=[p.name for p in upg], security=any(p.security for p in upg),
                    services=svc, busy=host_lock(h.id).locked(),
                ))
            out.append(dict(name=g.name, hosts=rows,
                            outdated=sum(1 for r in rows if r["upgradable"])))
    return out


@app.get("/api/upgradable")
def upgradable(user: str = Depends(current_user)):
    """更新可能な全パッケージをパッケージ名でまとめる。"""
    by_name: dict[str, dict] = {}
    with SessionLocal() as s:
        q = (s.query(Package, Host).join(Host, Package.host_id == Host.id)
             .filter(Host.enabled.is_(True), Package.upgradable.is_(True)))
        for p, h in q.all():
            e = by_name.setdefault(p.name, dict(name=p.name, group=p.group, security=False, hosts=[]))
            e["security"] = e["security"] or p.security
            e["hosts"].append(dict(host_id=h.id, host=h.name, vmid=h.vmid,
                                   installed=p.installed, candidate=p.candidate, security=p.security))
    return sorted(by_name.values(), key=lambda e: (not e["security"], -len(e["hosts"]), e["name"]))


@app.get("/api/containers")
def containers(user: str = Depends(current_user)):
    with SessionLocal() as s:
        q = (s.query(Container, Host).join(Host, Container.host_id == Host.id)
             .filter(Host.enabled.is_(True)).order_by(Host.vmid, Container.name))
        return [dict(id=c.id, host_id=h.id, host=h.name, vmid=h.vmid, name=c.name, image=c.image,
                     state=c.state, update_available=c.update_available,
                     compose_project=c.compose_project, compose_service=c.compose_service,
                     updatable=bool(c.compose_project and c.compose_service
                                    and c.compose_project != settings.self_compose_project),
                     note=("PatchDeck 自身のため画面からは更新しません" if c.compose_project == settings.self_compose_project
                           else c.note))
                for c, h in q.all()]


@app.get("/api/jobs")
def jobs(limit: int = 50, user: str = Depends(current_user)):
    with SessionLocal() as s:
        rows = s.query(Job).order_by(Job.id.desc()).limit(min(limit, 200)).all()
        return [dict(id=j.id, kind=j.kind, title=j.title, status=j.status, requested_by=j.requested_by,
                     created_at=iso(j.created_at), started_at=iso(j.started_at),
                     finished_at=iso(j.finished_at), summary=j.summary) for j in rows]


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int, user: str = Depends(current_user)):
    with SessionLocal() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(404)
        live = LIVE.get(job_id)
        return dict(id=j.id, kind=j.kind, title=j.title, status=j.status, requested_by=j.requested_by,
                    created_at=iso(j.created_at), started_at=iso(j.started_at),
                    finished_at=iso(j.finished_at), summary=j.summary,
                    log=live.text() if live else (j.log or ""))


# ---------------------------------------------------------------- 実行 API

class ScanReq(BaseModel):
    host_ids: list[int] | None = None


class PkgReq(BaseModel):
    host_ids: list[int] = Field(min_length=1)
    group: str | None = None
    packages: list[str] | None = None


class FullReq(BaseModel):
    host_ids: list[int] = Field(min_length=1)


class DockerReq(BaseModel):
    container_ids: list[int] = Field(min_length=1)


@app.post("/api/scan")
async def api_scan(req: ScanReq, user: str = Depends(require_write)):
    hosts = await asyncio.to_thread(load_hosts, req.host_ids)
    if not hosts:
        raise HTTPException(400, "対象ホストがありません")
    title = f"スキャン ({len(hosts)}台)" if len(hosts) > 1 else f"スキャン ({hosts[0].name})"
    job_id = await start_job("scan", title, lambda ctx: scan_many(hosts, ctx), user)
    return {"job_id": job_id}


def _resolve_pkg_targets(req: PkgReq) -> list[tuple[HostInfo, list[str]]]:
    if not req.group and not req.packages:
        raise HTTPException(400, "group か packages を指定してください")
    if req.group and not settings.group_by_name(req.group):
        raise HTTPException(400, f"未知のグループ: {req.group}")
    wanted = set(req.packages or [])
    bad = [p for p in wanted if not valid_pkg(p)]
    if bad:
        raise HTTPException(400, f"不正なパッケージ名: {bad}")
    out = []
    with SessionLocal() as s:
        for h in s.query(Host).filter(Host.id.in_(req.host_ids), Host.enabled.is_(True)).order_by(Host.vmid).all():
            names = sorted(p.name for p in h.packages if p.upgradable and
                           ((req.group and p.group == req.group) or p.name in wanted))
            if names:
                out.append((HostInfo(h.id, h.vmid, h.name, h.ip, h.port), names))
    return out


@app.post("/api/update/packages")
async def api_update_packages(req: PkgReq, user: str = Depends(require_write)):
    targets = await asyncio.to_thread(_resolve_pkg_targets, req)
    if not targets:
        raise HTTPException(400, "選ばれたホストに該当する更新がありません(先にスキャンしてください)")
    what = req.group or ", ".join(sorted(set(req.packages or [])))[:120]
    title = f"{what} を更新 ({len(targets)}台: {', '.join(h.name for h, _ in targets)[:200]})"

    async def run(ctx: JobContext):
        return list(await asyncio.gather(*(update_packages(h, pk, ctx) for h, pk in targets)))

    job_id = await start_job("pkg", title, run, user, _after_update)
    return {"job_id": job_id}


@app.post("/api/update/full")
async def api_update_full(req: FullReq, user: str = Depends(require_write)):
    hosts = [h for h in await asyncio.to_thread(load_hosts, req.host_ids)]
    if not hosts:
        raise HTTPException(400, "対象ホストがありません")
    title = f"apt upgrade ({len(hosts)}台: {', '.join(h.name for h in hosts)[:200]})"

    async def run(ctx: JobContext):
        return list(await asyncio.gather(*(full_upgrade(h, ctx) for h in hosts)))

    job_id = await start_job("full", title, run, user, _after_update)
    return {"job_id": job_id}


@app.post("/api/update/docker")
async def api_update_docker(req: DockerReq, user: str = Depends(require_write)):
    grouped = await asyncio.to_thread(load_containers, req.container_ids)
    # compose 管理のものだけに絞る(docker run 製は作り直しの引数が分からないので触らない)
    # PatchDeck 自身のコンテナは更新中に自分が落ちるので対象外
    grouped = {hid: (h, [c for c in cs if c.compose_project and c.compose_service
                         and c.compose_project != settings.self_compose_project])
               for hid, (h, cs) in grouped.items()}
    grouped = {hid: v for hid, v in grouped.items() if v[1]}
    if not grouped:
        raise HTTPException(400, "更新できるコンテナがありません(docker compose 管理のもののみ対応)")
    names = [c.name for _, cs in grouped.values() for c in cs]
    title = f"Docker 更新 ({', '.join(names)[:200]})"

    async def run(ctx: JobContext):
        return list(await asyncio.gather(*(update_containers(h, cs, ctx) for h, cs in grouped.values())))

    job_id = await start_job("docker", title, run, user, _after_update)
    return {"job_id": job_id}
