"""settings.yaml の対象ホストのホスト鍵を集めて known_hosts を作る。

使い方(コンテナ内):  python -m app.keyscan
既存の known_hosts に無いホストだけ追記します。鍵が変わったホストは警告だけ出して触りません。
"""
from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import settings


def host_token(ip: str, port: int) -> str:
    return ip if port == 22 else f"[{ip}]:{port}"


def main() -> int:
    if not settings.ssh_known_hosts:
        print("SSH_KNOWN_HOSTS が空なのでホスト鍵検証は無効です。何もしません。")
        return 0
    path = Path(settings.ssh_known_hosts)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text().splitlines() if path.exists() else []
    known = {line.split()[0] for line in existing if line.strip() and not line.startswith("#")}

    todo = [t for t in settings.targets if t.enabled and host_token(t.ip, t.port) not in known]

    def scan(t):
        r = subprocess.run(["ssh-keyscan", "-T", "5", "-p", str(t.port), "-t", "ed25519,ecdsa,rsa", t.ip],
                           capture_output=True, text=True)
        return t, [l for l in r.stdout.splitlines() if l and not l.startswith("#")]

    added = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(scan, todo))
    for t, lines in results:
        token = host_token(t.ip, t.port)
        if not lines:
            print(f"✗ {t.vmid} {t.name} ({token}): 取得できませんでした")
            continue
        with path.open("a") as f:
            f.write("\n".join(lines) + "\n")
        added += 1
        print(f"✓ {t.vmid} {t.name} ({token}) を追加")
    print(f"完了: {added} 台追加 → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
