#!/usr/bin/env bash
# PatchDeck 専用の SSH 公開鍵を、このノード上の LXC の root に配る(Proxmox ノードで実行)。
#
# 使い方:
#   bash install-key.sh "ssh-ed25519 AAAA... patchdeck"            # 鍵を追加(何度実行してもOK)
#   bash install-key.sh --remove "ssh-ed25519 AAAA... patchdeck"   # 鍵を削除
#
# 環境変数:
#   FROM=10.2.2.1   この接続元IPからだけ、その鍵を使えるようにする(既定 10.2.2.1)
#   IDS="101 107"   対象のCTを限定する(既定は下の一覧。このノードに無いCTは自動で飛ばす)
#
# ネットワークを通さず pct exec で書き込むので、ファイアウォールで22番が閉じていても使えます。
set -u

DEFAULT_IDS="101 102 103 104 105 106 107 109 111 113 121 127 129 130"
FROM="${FROM:-10.2.2.1}"
IDS="${IDS:-$DEFAULT_IDS}"

MODE=add
if [ "${1:-}" = "--remove" ]; then MODE=remove; shift; fi
PUB="${1:-}"

if ! command -v pct >/dev/null 2>&1; then
  echo "pct が見つかりません。Proxmox のノード(dell / NEC / surpro)で実行してください。" >&2
  exit 1
fi
case "$PUB" in
  ssh-ed25519\ *) ;;
  *) echo "使い方: $0 [--remove] \"ssh-ed25519 AAAA... コメント\"" >&2; exit 1 ;;
esac
case "$PUB" in *\"*|*\'*|*$'\n'*) echo "公開鍵に引用符や改行が入っています" >&2; exit 1 ;; esac
case "$FROM" in *[!0-9a-fA-F:./,*?!]*) echo "FROM の値が不正です: $FROM" >&2; exit 1 ;; esac

BODY=$(printf '%s' "$PUB" | awk '{print $2}')
LINE="from=\"$FROM\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 $BODY patchdeck"

INNER='
set -e
d=/root/.ssh; f=$d/authorized_keys
mkdir -p "$d"; chmod 700 "$d"; touch "$f"; chmod 600 "$f"
grep -vF -- "$1" "$f" > "$f.new" || true
if [ "$3" = add ]; then printf "%s\n" "$2" >> "$f.new"; fi
cat "$f.new" > "$f"; rm -f "$f.new"
if ! command -v sshd >/dev/null 2>&1 && [ ! -x /usr/sbin/sshd ]; then echo NOSSHD; fi
'

ok=0; skipped=0; failed=0
for id in $IDS; do
  st=$(pct status "$id" 2>/dev/null | awk '{print $2}')
  if [ -z "$st" ]; then skipped=$((skipped+1)); continue; fi   # このノードにはいないCT
  if [ "$st" != "running" ]; then echo "- $id: 停止中のため飛ばしました"; skipped=$((skipped+1)); continue; fi
  out=$(pct exec "$id" -- sh -c "$INNER" _ "$BODY" "$LINE" "$MODE" 2>&1) && rc=0 || rc=$?
  if [ $rc -ne 0 ]; then
    echo "✗ $id: 失敗 ($out)"; failed=$((failed+1))
  elif printf '%s' "$out" | grep -q NOSSHD; then
    echo "△ $id: 鍵は書きましたが sshd が入っていません (pct exec $id -- apt install -y openssh-server)"; ok=$((ok+1))
  else
    echo "✓ $id: $([ "$MODE" = add ] && echo 追加 || echo 削除)しました"; ok=$((ok+1))
  fi
done
echo "完了: 成功 $ok / 失敗 $failed / このノードに無い・停止中 $skipped"
