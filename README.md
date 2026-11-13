# PatchDeck

clusters-prj の各 LXC / VM に SSH して、Apache・Nginx などの更新状況をまとめて見て、画面からアップデートするためのツールです。

- **ホスト**: 各 CT/VM の更新件数・セキュリティ更新件数・サービス状態・Docker の更新有無の一覧。選んだホストを丸ごと `apt upgrade`
- **ソフト別**: 「Apache がどのCTで何バージョンか」を一覧にし、チェックしたホストで Apache だけ更新
- **パッケージ**: 更新可能な全パッケージ。選んだパッケージを該当ホスト全部で更新
- **Docker**: compose で動いているコンテナの新イメージ確認と `pull` → `up -d`
- **ジョブ**: 実行ログ(リアルタイム)と結果。更新ジョブの結果と定期スキャンの要約は Discord に通知

構成: FastAPI + MariaDB + asyncssh / 画面は素の HTML+JS

## 仕組み

1. `config/settings.yaml` の `targets` に書いたホストへ、root の鍵で SSH します(IP は VMID から自動計算)
2. スキャンでは `apt-get update` → `apt list --upgradable` → `dpkg-query` → `systemctl is-active`、Docker があれば `docker inspect` と `docker pull` で新イメージを確認します
3. 更新は以下を実行し、終わったらそのホストを再スキャンします
   - パッケージ単位: `apt-get install --only-upgrade -y <pkg...>`
   - 丸ごと: `apt-get upgrade -y`(`dist-upgrade` はしません)
   - Docker: `docker compose -p <project> -f <files> pull <svc> && ... up -d <svc>`
   - 設定ファイルの衝突は `--force-confold`(今の設定ファイルを残す)で自動回答します
4. 定期スキャン(既定 6 時間ごと)で、セキュリティ更新やエラーの状況が前回から変わったときだけ Discord に通知します

## セットアップ(Portainer)

イメージは GitHub Actions が `main` への push ごとにビルドして `ghcr.io/swmr71/patchdeck:latest` に置きます(`v1.2.3` のタグを打つと `1.2.3` / `1.2` も付きます)。

1. 201 で鍵と設定の置き場を作る
   ```bash
   mkdir -p /opt/patchdeck/ssh /opt/patchdeck/config
   cp /path/to/id_ed25519 /opt/patchdeck/ssh/id_ed25519
   chmod 700 /opt/patchdeck/ssh && chmod 600 /opt/patchdeck/ssh/id_ed25519
   ```
2. Portainer → Stacks → Add stack。名前は `patchdeck`、Web editor に `portainer-stack.yml` を貼る
3. 下の Environment variables に入れる

   | 名前 | 内容 |
   |---|---|
   | `DB_PASSWORD` | 必須。長いランダム文字列 |
   | `DISCORD_WEBHOOK_URL` | 通知先(空なら通知なし) |
   | `DISCORD_MENTION_USER_ID` | 失敗時にメンションする人(任意) |
   | `CF_ACCESS_AUD` | Cloudflare Access の AUD(公開前に必須、下記) |

4. Deploy。初回起動時に `/opt/patchdeck/config/settings.yaml` のひな形が置かれ、対象ホストのホスト鍵が `/opt/patchdeck/ssh/known_hosts` に自動登録されます
5. 対象ホストを変えたいときは `/opt/patchdeck/config/settings.yaml` を編集して、Portainer で app コンテナを Restart

新しいバージョンに上げるときは、スタックの「Update the stack」で **Re-pull image** にチェックを入れて更新します。

## セットアップ(docker compose で手元ビルド)

```bash
# 1. ファイルを置く
scp -r patchdeck root@10.2.2.1:/opt/
ssh root@10.2.2.1
cd /opt/patchdeck

# 2. SSH 鍵(各CTに入れる root の鍵)を置く
mkdir -p ssh
cp /path/to/id_ed25519 ssh/id_ed25519
chmod 700 ssh && chmod 600 ssh/id_ed25519

# 3. 設定
cp .env.example .env
nano .env                    # DB_PASSWORD と DISCORD_WEBHOOK_URL を入れる
nano config/settings.yaml    # 対象ホストを確認

# 4. ビルドして起動(起動時に対象ホストのホスト鍵を known_hosts に自動登録します)
docker compose up -d --build
docker compose logs -f app
```

`http://10.2.2.1:8800` で開けます。

### Cloudflare Tunnel / Access で公開する

1. ProxmoxTunnel1 に公開ホスト名を追加: `patchdeck.clusters-prj.com` → `http://10.2.2.1:8800`
2. Zero Trust で Access アプリを作り、ポリシーは「管理者を許可」
3. Access アプリの **Application Audience (AUD) Tag** を `.env` の `CF_ACCESS_AUD` に入れて `docker compose up -d`

`CF_ACCESS_AUD` を入れると、Cloudflare Access の JWT が無いリクエストは全部 401/403 になります(LAN から直接 `:8800` を開いても弾かれます)。**root で全サーバーを触れるツールなので、公開する前に必ず設定してください。** ジョブには Access のメールアドレスが「実行者」として記録されます。

## 設定

### 対象ホストを増やす・外す

`config/settings.yaml` の `targets` を編集して `docker compose restart app`。
新しく足したホストのホスト鍵は、再起動時に自動で登録されます(`KEYSCAN_ON_STARTUP=false` で無効化)。
消したホストは DB 上で無効化され、画面から消えます。

### 見張るソフトを増やす

`watch` に足します。`packages` はワイルドカード可で、一致する更新可能パッケージをまとめて上げます。

```yaml
  - name: Grafana
    packages: ["grafana", "grafana-*"]
    service: grafana-server
```

### ホスト鍵が変わったとき(CT を作り直したなど)

`ssh/known_hosts` から該当 IP の行を消して、app コンテナを再起動します。
鍵が合わないホストは「接続/実行エラー: HostKeyNotVerifiable」になります。

## 注意点

- **apt 系(Debian/Ubuntu)のみ対応**です。それ以外は「未対応」と表示されます
- サービスの再起動はパッケージ側(`apt` のメンテナスクリプト)任せです。Apache などは更新時に自動で再起動されます
- `docker run` で直接作ったコンテナは作り直しの引数が分からないので、確認だけで更新はしません
- PatchDeck 自身の compose プロジェクト(`SELF_COMPOSE_PROJECT`、既定 `patchdeck`)は画面から更新できないようにしています。Portainer のスタック名を変えたら合わせてください
- 201 自体の `apt upgrade` で docker-ce が上がると、Docker が再起動して PatchDeck も落ちます。そのジョブは次の起動時に「中断」として失敗扱いになります(更新自体は進んでいるので、再スキャンで確認してください)
- 毎週木曜早朝のルーター再起動と定期スキャンが重なると、その回は接続エラーになります
- 同時に作業するホスト数は `MAX_PARALLEL`(既定 3)。dell のメモリが厳しいので上げすぎ注意

## 開発

```bash
pip install -r requirements.txt pytest
python -m pytest tests
# SQLite で手元起動(SSH_KNOWN_HOSTS= を空にするとホスト鍵検証なし)
CONFIG_PATH=config/settings.yaml DATABASE_URL=sqlite:///./dev.db SSH_KEY_PATH=~/.ssh/id_ed25519 \
  SSH_KNOWN_HOSTS=~/.ssh/known_hosts uvicorn app.main:app --reload --port 8800
```
