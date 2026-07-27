# 外出先からの接続ガイド（Tailscale / VPN）

自宅Wi-Fiの外（モバイル回線・外出先のWi-Fi）にいるスマホから ToDoChat に接続するための、**Tailscale の導入手順**と**接続方法**をまとめる。同一Wi-Fi内だけで使う場合は [`LAN接続.md`](./LAN接続.md) を参照。

> **なぜ Tailscale か**
> ポート開放でインターネットに直接公開する方式は、経路が暗号化されず攻撃面も大きいため**採用しない**。
> Tailscale は PC とスマホを同じ**仮想ネットワーク（tailnet）**に入れる VPN で、通信は端末間で暗号化（WireGuard）される。外からでも「同じLANにいる」かのように `100.x.y.z` のアドレスで PC に届く。ルーター設定・グローバルIP・DDNS すべて不要。

---

## 仕組みの概要

- Tailscale をインストールした端末どうしは、同じアカウントでログインするだけで**tailnet**という専用の仮想ネットワークで繋がる。
- 各端末には `100.64.0.0/10`（CGNAT帯）の**Tailscale IP**（例：`100.101.102.103`）が割り当てられる。このアドレスは外出先でも変わらず PC を指す。
- ToDoChat 本体は LANモード（`0.0.0.0` 待受・既定）なら Tailscale の仮想アダプタでも自動で受かるため、**サーバー側の設定変更は不要**。スマホから `http://<PCのTailscale IP>:8765/` で接続する。
- 接続トークン認証は Tailscale 経由でも有効（PC本体＝loopback以外はトークン必須）。初回だけトークン付きURLでログインする。
- 確認カード（Bash実行の許可待ち）のフックコールバックは `HOST=127.0.0.1` 固定でPC内ループバックから叩くため、Tailscale接続中でも無傷で動作する。

> **実機検証済み（外出先相当のスマホ接続）**：Tailscale IP でのトークンログイン → 会話往復 → 🔄同期 → 🔧確認カードの許可→実行 まで一通り成功を確認済み。Tailscaleアダプタは「プライベート」分類のためファイアウォールの追加設定は不要だった。

> **設定ウィンドウのQR対応**
> ⚙️設定ウィンドウの「接続トークン」欄は、**「📶 同一Wi-Fi用（LAN）」と「🌐 外出先用（Tailscale VPN）」の2種類のURL＋QR**を出し分けて表示する。外出先用はサーバーが Tailscale IP（`100.x`）を自動検出して生成するので、スマホでそのQRを読むだけで初回認証が済む（手動でのIP確認は不要）。Tailscaleが未導入・未起動のときは外出先用の枠に案内が出る。

---

## ステップA：PC に Tailscale を導入

1. 公式サイト <https://tailscale.com/download/windows> から Windows 版インストーラーをダウンロードして実行する。
2. インストール後、タスクトレイの Tailscale アイコンから **「Log in」** を選び、ブラウザで認証する。
   - アカウントは Google / Microsoft / GitHub などで作成できる（無料の Personal プランで個人利用は十分）。
3. ログインすると tailnet に参加し、この PC に Tailscale IP が割り当てられる。
4. PCのTailscale IP を確認しておく（後でスマホから使う）。コマンドプロンプト or PowerShell で：
   ```
   tailscale ip -4
   ```
   → `100.x.y.z` が表示される。これが**外出先からPCを指すアドレス**。

> **PC は常時オンラインである必要がある**：スマホから繋ぐ瞬間に PC が起動しており、Tailscale が動いていること。スリープ中は繋がらない（スリープ抑止やWoLは別途）。

---

## ステップB：スマホに Tailscale を導入

1. スマホに Tailscale アプリをインストールする。
   - iPhone：App Store で「Tailscale」
   - Android：Google Play で「Tailscale」
2. アプリを開き、**PCと同じアカウント**でログインする。
3. VPN 接続を許可する（初回に OS の VPN 構成の追加を求められるので許可）。トグルを ON にすると tailnet に参加する。
4. これでスマホと PC が同じ tailnet に入り、モバイル回線でも PC に届くようになる。

---

## ステップC：ToDoChat 側の準備（同一Wi-Fi接続と共通）

- ⚙️設定ウィンドウ →「接続モード」が **「LANモード」（既定）** になっていることを確認する。ローカル限定だと Tailscale 経由でも繋がらない。
- ファイアウォールの受信許可ルールは、LANモード時に自動追加される（[`LAN接続.md`](./LAN接続.md) 参照）。
  - 既定ルールは `profile=private`。**Tailscale の仮想アダプタは Windows 上で「プライベート」に分類される（実機確認済み）ため、この既定ルールがそのまま適用され、Tailscale 用の追加設定は不要。** 万一「パブリック」判定になっていると弾かれるので、その場合のみ下記トラブルシューティングを参照。
- 起動中の ToDoChat があれば全ウィンドウ・cmd窓を閉じてから `start.bat` で起動し直す。

---

## ステップD：スマホから接続（初回はトークン認証）

1. スマホの Tailscale トグルが **ON** になっていることを確認する（外出先ではモバイル回線でOK）。
2. **一番かんたん（QR）**：PC本体の ⚙️設定ウィンドウ →「接続トークン」→ **「🌐 外出先用（Tailscale VPN）」のQR**をスマホのカメラで読む。トークン付きURLが開き、サーバーがCookieを発行してトップ画面へ遷移すれば**ログイン完了**。
   - サーバーが PCのTailscale IP（`100.x`）を自動検出してこのQR/URLを生成するので、IPの手動確認は不要。
3. **URLを手打ちする場合**：同じ欄に表示される「外出先用のスマホ用URL」をスマホのブラウザに入力しても同じ（`http://<PCのTailscale IP>:8765/?token=<接続トークン>` の形）。
4. **2回目以降**：`http://<PCのTailscale IP>:8765/` を開くだけ（Cookieが残っていればトークン不要）。ブックマーク推奨。
5. トークンが漏れた恐れがあるときは、設定ウィンドウの **🔑再生成** で無効化・再発行できる（QR/URLも新トークンに更新される）。

> **同一Wi-Fi内では従来どおり**：家にいるときはLAN IP（`192.168.x.x`）で速く繋がる。外出先だけ Tailscale IP に切り替える運用でよい。Tailscale を常時ONにしておけば、家でも外でも `100.x.y.z` の一本で繋ぎ続けることもできる。

---

## トラブルシューティング

| 症状 | 確認ポイント |
|---|---|
| Tailscale IP で開けない | スマホ・PC双方の Tailscale トグルが ON か。同じアカウントで両方ログインしているか（`tailscale status` に相手が出るか） |
| `tailscale status` に相手端末が出ない | 別アカウントでログインしている。同一アカウントに揃える |
| 疎通はするがブラウザで開けない | ToDoChat が LANモード（`0.0.0.0`）で起動しているか。`netstat -ano | findstr :8765` が `0.0.0.0:8765` か |
| `0.0.0.0` なのに繋がらない | 通常はFWは問題ない（Tailscaleアダプタはプライベート分類で既定ルールが効く）。稀に「パブリック」判定だと `profile=private` ルールで弾かれる。下記参照 |
| ログイン画面が繰り返し出る | トークンが違う／Cookieが保存されない（プライベートブラウズ等）。設定ウィンドウの正しいトークンで `?token=` 付きURLを開き直す |
| 家では繋がるが外で繋がらない | スマホの Tailscale が OFF、または PC がスリープ・シャットダウン |

### ファイアウォールが Tailscale 経由を弾く場合

現状の自動ルールは `profile=private` 限定。Tailscale の仮想アダプタが「パブリック ネットワーク」扱いだと受信が許可されない。対処のいずれか：

- **A（推奨）**：Windows の「ネットワークと共有センター」で Tailscale の接続を**プライベート**に変更する。
- **B**：管理者権限の cmd で、パブリックも許可するルールを追加する（LAN公開の危険が増える点に留意。Tailscale IP は tailnet 外からは到達しないため実害は限定的だが、公共Wi-Fi直下では注意）：
  ```
  netsh advfirewall firewall add rule name="ToDoChat-Tailscale" dir=in action=allow protocol=TCP localport=8765 profile=private,public
  ```
  不要になったら削除：
  ```
  netsh advfirewall firewall delete rule name="ToDoChat-Tailscale"
  ```

---

## スマホで全画面（PWA・standalone）にする — Tailscale HTTPS 配信

ホーム画面に追加したToDoChatを**アドレスバーなしの全画面**で開くには、HTTPSでの配信が必要（Service Worker は HTTPS か localhost でしか登録できず、平文HTTPだとAndroidはブラウザのショートカット扱いになる）。Tailscale の `serve` を使うと、証明書の発行・更新を Tailscale に任せてHTTPS化できる。

### 一度だけの準備
1. **管理コンソールで「HTTPS Certificates」を有効化**する（[https://login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns) の「HTTPS Certificates」）。あわせて **MagicDNS** も有効にしておく。
   - 未有効のままだと `tailscale serve` が証明書を取得できず起動時にブロックする（ToDoChatは時間上限で自衛するので起動自体は止まらないが、HTTPSは繋がらない）。

### 使い方
- ToDoChat は **LANモードでの起動時に自動で** `tailscale serve --bg --https=443 http://127.0.0.1:8765` を実行してHTTPS配信を用意する（既に設定済みなら何もしない）。手動でやる場合も同じコマンドでよい。
- ⚙️設定ウィンドウの接続トークン欄の **「🔒 全画面PWA用（Tailscale HTTPS）」** に出る `https://<PC名>.<tailnet>.ts.net/` を、Tailscale をONにしたスマホで開く → メニューから「ホーム画面に追加」で**全画面PWA**になる。
- このHTTPS URLは **トークン不要**（Tailscale がTLS終端し、ToDoChatにはPC内のloopbackとして届くため。tailnetに参加している端末だけが到達できる）。
- 解除したいときは `tailscale serve reset`。

---

## セキュリティ上の注意

- Tailscale 経由の通信は WireGuard で暗号化されるため、平文HTTPでも tailnet 外からは覗けない。**LANの平文HTTPより外出先向けとして安全**。
- ただし tailnet に招待した端末は PC に届くため、**共有アカウント・不要な端末を tailnet に入れない**こと。
- 接続トークンは引き続き必須。漏れたら 🔑再生成で無効化する。

---

## 実装済みメモ

- ⚙️設定ウィンドウの接続トークン欄は、LAN用に加えて **Tailscale IP を自動検出して外出先用のURL＋QRを併記**する（サーバー側で `tailscale ip -4` を取得し、失敗時は自ホストのアドレスから `100.64.0.0/10` を走査。`app/server.py` の `_tailscale_ip()`）。外出先接続時にTailscale IPを手で調べる必要はない。
- **全画面PWA用のHTTPS配信**は、起動時にLANモードなら `_ensure_tailscale_serve()` が別スレッドで `tailscale serve --bg --https=443 http://127.0.0.1:PORT` をベストエフォート実行する（`_serve_already_fronts_port()` で既設定なら即スキップ）。MagicDNS名は `_ts_dns_name()`（`tailscale status --json` の `Self.DNSName`）で取得し、`tailscale_serve_url()` が `https://<MagicDNS名>/` を組み立てて⚙️設定へ渡す。
  - serve 経由はTLS終端後に `127.0.0.1` からプロキシされるため、現状の `_authenticate()` のloopback無条件許可により**トークン認証はかからない**（tailnet参加端末のみ到達可という前提での割り切り）。より厳密にするなら serve 付与ヘッダで経由を判定してトークンを要求する改修が将来タスク。
