#!/bin/sh
# PatchDeck 起動前の準備: 設定ファイルの初期配置・SSH鍵の確認・ホスト鍵の自動登録
set -e

CONFIG_PATH="${CONFIG_PATH:-/config/settings.yaml}"
SSH_KEY_PATH="${SSH_KEY_PATH:-/ssh/id_ed25519}"

# 設定ファイルが無ければ同梱のひな形を置く(以降はこのファイルを編集して再起動)
if [ ! -f "$CONFIG_PATH" ]; then
  mkdir -p "$(dirname "$CONFIG_PATH")"
  cp /srv/defaults/settings.yaml "$CONFIG_PATH"
  echo "[patchdeck] $CONFIG_PATH が無かったので、ひな形を置きました。対象ホストを確認して再起動してください。"
fi

if [ ! -f "$SSH_KEY_PATH" ]; then
  echo "[patchdeck] 警告: SSH 秘密鍵 $SSH_KEY_PATH がありません。スキャンはすべて失敗します。"
fi

# known_hosts に無いホストだけ鍵を取ってくる(既に登録済みの鍵は変えない)
if [ "${KEYSCAN_ON_STARTUP:-true}" = "true" ] && [ -n "${SSH_KNOWN_HOSTS-/ssh/known_hosts}" ]; then
  python -m app.keyscan || echo "[patchdeck] keyscan に失敗しましたが、起動は続けます"
fi

exec "$@"
