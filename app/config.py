"""設定まわり。環境変数(.env) と config/settings.yaml を読む。"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def vmid_to_ip(vmid: int) -> str:
    """clusters-prj の IP 規則: 10.2.(VMIDの百の位以上).(下2桁)"""
    return f"10.2.{vmid // 100}.{vmid % 100}"


@dataclass
class Target:
    vmid: int
    name: str
    ip: str
    port: int = 22
    enabled: bool = True


@dataclass
class WatchGroup:
    """「Apache」「Nginx」のような、まとめて扱うソフトの単位。"""
    name: str
    packages: list[str]          # パッケージ名のパターン(fnmatch)。更新時はこれに一致するものをまとめて上げる
    service: str | None = None   # systemd ユニット名。"$pkg" なら一致したパッケージ名をそのまま使う

    def matches(self, pkg: str) -> bool:
        return any(fnmatch.fnmatchcase(pkg, p) for p in self.packages)

    def primary_rank(self, pkg: str) -> int:
        """表示用バージョンをどのパッケージから取るかの優先度(小さいほど優先)。"""
        for i, p in enumerate(self.packages):
            if fnmatch.fnmatchcase(pkg, p):
                return i
        return 10_000


@dataclass
class Settings:
    database_url: str
    ssh_user: str
    ssh_key_path: str
    ssh_known_hosts: str | None
    ssh_connect_timeout: int
    cmd_timeout: int
    max_parallel: int
    scan_interval_hours: float
    scan_on_startup: bool
    docker_check_pull: bool
    discord_webhook_url: str
    discord_mention_user_id: str
    cf_access_team_domain: str
    cf_access_aud: str
    base_url: str
    self_compose_project: str
    config_path: Path
    targets: list[Target] = field(default_factory=list)
    watch: list[WatchGroup] = field(default_factory=list)

    def group_of(self, pkg: str) -> WatchGroup | None:
        for g in self.watch:
            if g.matches(pkg):
                return g
        return None

    def group_by_name(self, name: str) -> WatchGroup | None:
        for g in self.watch:
            if g.name == name:
                return g
        return None


def load_settings() -> Settings:
    config_path = Path(_env("CONFIG_PATH", "config/settings.yaml"))
    data = {}
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    targets = []
    for t in data.get("targets", []) or []:
        vmid = int(t["vmid"])
        targets.append(Target(
            vmid=vmid,
            name=str(t.get("name") or vmid),
            ip=str(t.get("ip") or vmid_to_ip(vmid)),
            port=int(t.get("port") or 22),
            enabled=bool(t.get("enabled", True)),
        ))

    watch = []
    for w in data.get("watch", []) or []:
        pk = w.get("packages") or []
        if isinstance(pk, str):
            pk = [pk]
        watch.append(WatchGroup(name=str(w["name"]), packages=[str(p) for p in pk], service=w.get("service")))

    known_hosts = _env("SSH_KNOWN_HOSTS", "/ssh/known_hosts")
    return Settings(
        database_url=_env("DATABASE_URL", "sqlite:///./patchdeck.db"),
        ssh_user=_env("SSH_USER", "root"),
        ssh_key_path=_env("SSH_KEY_PATH", "/ssh/id_ed25519"),
        # 空文字にするとホスト鍵検証を無効化(非推奨)
        ssh_known_hosts=known_hosts or None,
        ssh_connect_timeout=int(_env("SSH_CONNECT_TIMEOUT", "10")),
        cmd_timeout=int(_env("CMD_TIMEOUT", "1800")),
        max_parallel=int(_env("MAX_PARALLEL", "3")),
        scan_interval_hours=float(_env("SCAN_INTERVAL_HOURS", "6")),
        scan_on_startup=_env_bool("SCAN_ON_STARTUP", True),
        docker_check_pull=bool(data.get("docker", {}).get("check_with_pull", True)),
        discord_webhook_url=_env("DISCORD_WEBHOOK_URL"),
        discord_mention_user_id=_env("DISCORD_MENTION_USER_ID"),
        cf_access_team_domain=_env("CF_ACCESS_TEAM_DOMAIN", "clusters-prj.cloudflareaccess.com"),
        cf_access_aud=_env("CF_ACCESS_AUD"),
        base_url=_env("BASE_URL"),
        self_compose_project=_env("SELF_COMPOSE_PROJECT", "patchdeck"),
        config_path=config_path,
        targets=targets,
        watch=watch,
    )


settings = load_settings()
