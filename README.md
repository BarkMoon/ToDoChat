# ToDoChat

「ToDoChat」は、PC を立ち上げてチャットするだけでアプリケーション開発を進められる、めんどくさがりな人のための開発ツールです。ヘッドレスの Claude CLI をローカル Web アプリでラップし、隙間時間に即座に開発へ取り掛かれることを目指しています。

## 特長

- **チャットで開発** — 会話するだけでコードの調査・編集・コマンド実行まで行える。AI の応答は `stream-json` で逐次表示され、生成の様子がリアルタイムに見える。
- **3つの動作モード** — 「助言のみ」「編集」「実行（都度確認）」を切替。
  - 編集モードは Edit/Write を作業フォルダ内に限定（PreToolUse フック `app/edit_guard.py` で強制）。
  - 実行モードは Bash（シェル・アプリ実行）を1コマンドずつ UI で許可/拒否。読み取り専用の安全コマンド（`git status`・`head` など）は確認カードを出しつつ自動許可（`app/safe_shell.py` / `app/safe_commands.txt`）。
  - 許可カードにはコマンドの意図・処理内容（CLI が付す `description`）を併記。
- **モデル選択** — 送信ごとに Opus / Sonnet / Haiku を切替可能。
- **記憶ログ（引き継ぎ）** — 引き継ぐべき進行中状態を要約ノートとして `.todochat/memory/` に保存し、次回起動時の挨拶に注入。サーバー再起動をまたいで文脈を維持。トークンを浪費するフルログ復元とは別系統の軽量な仕組み。
- **フルログ復元（任意）** — トグル `📜 フルログ復元` を ON にすると `--resume` で前回の会話全体を復元して深掘り作業を継続。
- **複数プロジェクト管理** — 作業フォルダをネイティブ選択ダイアログで追加・切替・削除（`projects.json` に保存）。
- **端末間同期** — 各ターンを `.todochat/transcripts/` に記録し、🔄同期ボタンでスマホ⇔PC など別端末に会話を引き継ぎ。
- **LAN 接続** — 既定で LAN 公開し、同一ネットワークのスマホ・別 PC からアクセス可能。⚙️設定ウィンドウで LAN／ローカル限定を切替し、Windows ファイアウォールの受信許可を自動同期。詳細は [`docs/LAN接続.md`](docs/LAN接続.md)。
- **独立ウィンドウ** — Edge/Chrome の専用プロファイル＋`--app` モードでタブ・アドレスバー無しの単独ウィンドウとして起動。
- **Windows スタートアップ登録** — ヘッダのトグルからログオン時自動起動を登録/解除（タスクスケジューラ方式）。
- **使用モデル記録** — ファイル編集に貢献したモデルを `.todochat/models/` に記録し、コミットメッセージ末尾の `(モデル名)` へ反映。
- **トークン使用量の可視化** — コンテキスト使用量バーと累計を表示。

## 導入（初回セットアップ）

ToDoChat はヘッドレスの **Claude CLI（`claude.exe`）をラップして動く**ため、事前に CLI 本体のインストールと認証が必要です。

1. **Claude CLI をインストール**
   PowerShell で公式インストーラを実行します。既定で `%USERPROFILE%\.local\bin\claude.exe` に入り、ToDoChat はこのパスを自動で参照します（見つからない場合は PATH 上の `claude` を使用）。
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
   インストール確認:
   ```powershell
   & "$env:USERPROFILE\.local\bin\claude.exe" --version
   ```

2. **Claude アカウントで認証（ログイン）**
   ```powershell
   & "$env:USERPROFILE\.local\bin\claude.exe" /login
   ```
   Pro プラン等のアカウントでログインすると、以降 API 課金なしで CLI を利用できます。

3. **Python を用意**
   Python 3.8 以上をインストールし、`python` がコマンドから実行できる状態にします（ToDoChat のサーバーは標準ライブラリのみで動作し、追加パッケージは不要）。

4. **ToDoChat を起動**
   `start.bat` を実行します（下記「起動方法」）。

## 起動方法

`start.bat` を実行するとサーバーが立ち上がり、専用ウィンドウでアプリが開きます（既定ポート `8765`）。

- 待受アドレスは ⚙️設定ウィンドウの「接続モード」で切替（次回起動時に反映）。
- 環境変数 `TODOCHAT_HOST` / `TODOCHAT_PORT` で上書き可能（設定より優先）。

> ⚠️ **セキュリティ**: 現時点で認証機構は無いため、ポートに到達できる人は誰でも CLI を操作できます。LAN モードは信頼できるネットワーク（自宅 Wi-Fi / VPN）でのみ利用してください。

## チャットコマンド

入力欄に送信するスラッシュコマンドでアプリ操作を行えます（`/startup` `/new` `/save` `/refresh` `/remember` `/clear` の6種）。各コマンドの動作と使い分けは [`docs/コマンド一覧.md`](docs/コマンド一覧.md) を参照。

## 構成

| ファイル | 役割 |
|---|---|
| `app/server.py` | HTTP サーバー本体。CLI 起動・許可制御・記憶ログ・各種 API。バージョンは `APP_VERSION` で管理 |
| `app/index.html` | Web UI（チャット・設定・各種モーダル） |
| `app/edit_guard.py` | PreToolUse フック（編集を作業フォルダ内に限定） |
| `app/safe_shell.py` / `app/safe_commands.txt` | 読み取り専用コマンドの安全判定と許可リスト |
| `projects.json` | プロジェクト一覧・各種設定の永続化 |
| `.todochat/` | プロジェクトごとの記憶ログ・フルログ・セッション等（gitignore 済み） |

## 要件

- Windows
- Python 3.8+
- ヘッドレス Claude CLI（Pro 認証）
