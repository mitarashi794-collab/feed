# マイフィード

Android の Chrome で使う、自分専用の情報収集PWAです。

- 画面：`index.html` / `styles.css` / `app.js` / `manifest.json` / `service-worker.js` / `icon.svg`
- 情報源：`sources.json`
- 収集：`scripts/fetch_feeds.py`（GitHub Actions が1時間ごとに実行）
- 公開データ：`data/feed.json`（自動更新）

APIキー・パスワード・Cookie は、このリポジトリに置かないでください（公開リポジトリです）。
