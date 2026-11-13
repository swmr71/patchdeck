"""リモートコマンド出力のパーサ(副作用なし・テストしやすいよう分離)。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# 例: apache2/stable-security 2.4.62-1~deb12u2 amd64 [upgradable from: 2.4.62-1~deb12u1]
# Ubuntu: openssl/noble-updates,noble-security 3.0.13-0ubuntu3.5 amd64 [upgradable from: 3.0.13-0ubuntu3.4]
_UPG_RE = re.compile(r"^(?P<name>[^/\s]+)/(?P<suite>\S+)\s+(?P<cand>\S+)\s+(?P<arch>\S+)\s+\[upgradable from: (?P<inst>[^\]]+)\]")


@dataclass
class Upgradable:
    name: str
    suite: str
    candidate: str
    installed: str
    security: bool


def parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_apt_upgradable(text: str) -> list[Upgradable]:
    result = []
    for line in text.splitlines():
        m = _UPG_RE.match(line.strip())
        if not m:
            continue
        suite = m.group("suite")
        result.append(Upgradable(
            name=m.group("name"),
            suite=suite,
            candidate=m.group("cand"),
            installed=m.group("inst").strip(),
            security="security" in suite,
        ))
    return result


def parse_dpkg_installed(text: str) -> dict[str, str]:
    """dpkg-query -W -f='${Package}\\t${Version}\\t${db:Status-Abbrev}\\n' の出力 → {pkg: version}"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, ver, status = parts[0], parts[1], parts[2]
        if status.startswith("ii"):
            out[name] = ver
    return out


def parse_systemctl_is_active(units: list[str], text: str) -> dict[str, str]:
    lines = [l.strip() for l in text.splitlines()]
    out = {}
    for i, u in enumerate(units):
        out[u] = lines[i] if i < len(lines) and lines[i] else "unknown"
    return out


@dataclass
class DockerContainer:
    name: str
    image: str
    image_id: str
    state: str
    compose_project: str | None
    compose_service: str | None
    compose_dir: str | None
    compose_files: str | None


def parse_docker_inspect(text: str) -> list[DockerContainer]:
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    out = []
    for c in data:
        cfg = c.get("Config") or {}
        labels = cfg.get("Labels") or {}
        state = c.get("State") or {}
        out.append(DockerContainer(
            name=(c.get("Name") or "").lstrip("/"),
            image=cfg.get("Image") or "",
            image_id=c.get("Image") or "",
            state=state.get("Status") or "",
            compose_project=labels.get("com.docker.compose.project"),
            compose_service=labels.get("com.docker.compose.service"),
            compose_dir=labels.get("com.docker.compose.project.working_dir"),
            compose_files=labels.get("com.docker.compose.project.config_files"),
        ))
    return out
