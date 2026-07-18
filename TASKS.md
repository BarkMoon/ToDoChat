# ToDoChat タスク

## 進行中
- （なし）

## 次の候補
- [ ] AIに編集系ツール（Edit/Write/Bash）を安全に許可する仕組み（現状はRead/Glob/Grepのみの助言モード）
- [ ] 会話履歴の永続化（プロジェクトごと・サーバ再起動後も保持）
- [ ] Windowsスタートアップへの登録
- [ ] 使用感の評価（良くなければUIをTauri等へ変更検討）

## 完了
- [x] アーキテクチャ方針の決定（Pattern A: claude CLIラッパー）
- [x] ヘッドレス claude CLI の導入と動作確認
- [x] ローカルWeb版の最小プロトタイプ（server.py / index.html / start.bat）
- [x] UI改善: 作業フォルダ名表示・AI返信のMarkdown整形・トークン使用量表示
- [x] 作業フォルダの設定/切替（一覧を projects.json に保存・GUIで追加/切替/削除）
- [x] トークン使用量のグラフ化（コンテキスト使用量バー＋累計表示）
- [x] フォルダ追加をネイティブ選択ダイアログ化（パス手入力を廃止）
- [x] メッセージ送信ごとのモデル選択（Opus/Sonnet/Haiku）
