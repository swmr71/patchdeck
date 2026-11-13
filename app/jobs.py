"""バックグラウンドジョブの実行管理(ライブログ・ホストごとのロック・並列数制限)。"""
from __future__ import annotations

import asyncio
import logging
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from .config import settings
from .db import Job, SessionLocal, utcnow

log = logging.getLogger("patchdeck.jobs")


@dataclass
class HostResult:
    host: str
    ok: bool
    message: str

    def line(self) -> str:
        return f"{'✅' if self.ok else '❌'} {self.host}: {self.message}"


class JobContext:
    def __init__(self, job_id: int, kind: str, title: str):
        self.job_id = job_id
        self.kind = kind
        self.title = title
        self.lines: list[str] = []

    def log(self, line: str, host: str | None = None) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = f"[{host}] " if host else ""
        self.lines.append(f"{ts} {prefix}{line}")
        # メモリ肥大化防止(apt の出力は長くなりがち)
        if len(self.lines) > 20000:
            del self.lines[:5000]

    def text(self) -> str:
        return "\n".join(self.lines)


LIVE: dict[int, JobContext] = {}
_host_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_semaphore: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()


def host_lock(host_id: int) -> asyncio.Lock:
    return _host_locks[host_id]


def semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.max_parallel))
    return _semaphore


def _create_job(kind: str, title: str, requested_by: str | None) -> int:
    with SessionLocal() as s:
        job = Job(kind=kind, title=title, status="queued", requested_by=requested_by, created_at=utcnow())
        s.add(job)
        s.commit()
        return job.id


def _mark_running(job_id: int) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        job.status = "running"
        job.started_at = utcnow()
        s.commit()


def _finish(job_id: int, status: str, summary: str, log_text: str) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        job.status = status
        job.finished_at = utcnow()
        job.summary = summary
        job.log = log_text
        s.commit()


def mark_interrupted_jobs() -> None:
    """起動時: 前回プロセスで実行中のまま終わったジョブを失敗扱いにする。"""
    with SessionLocal() as s:
        for job in s.query(Job).filter(Job.status.in_(["queued", "running"])).all():
            job.status = "failed"
            job.finished_at = utcnow()
            job.summary = (job.summary or "") + "\n(ツールの再起動で中断されました)"
        s.commit()


JobFn = Callable[[JobContext], Awaitable[list[HostResult]]]
AfterFn = Callable[[JobContext, str, list[HostResult]], Awaitable[None]]


async def _run(ctx: JobContext, fn: JobFn, after: AfterFn | None) -> None:
    await asyncio.to_thread(_mark_running, ctx.job_id)
    results: list[HostResult] = []
    try:
        results = await fn(ctx)
        if not results:
            status = "success"
        elif all(r.ok for r in results):
            status = "success"
        elif any(r.ok for r in results):
            status = "partial"
        else:
            status = "failed"
    except Exception:  # noqa: BLE001
        ctx.log("ジョブ内で予期しないエラー:\n" + traceback.format_exc())
        status = "failed"
    summary = "\n".join(r.line() for r in results)
    await asyncio.to_thread(_finish, ctx.job_id, status, summary, ctx.text())
    LIVE.pop(ctx.job_id, None)
    if after:
        try:
            await after(ctx, status, results)
        except Exception:  # noqa: BLE001
            log.exception("after-hook failed")


async def start_job(kind: str, title: str, fn: JobFn, requested_by: str | None = None,
                    after: AfterFn | None = None) -> int:
    job_id = await asyncio.to_thread(_create_job, kind, title, requested_by)
    ctx = JobContext(job_id, kind, title)
    LIVE[job_id] = ctx
    task = asyncio.create_task(_run(ctx, fn, after))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job_id
