VYDEO 修正版 — 2026-10-16

修正内容
- Dockerビルド時に yt-dlp を最新の公開版に更新
- 422の原因を日本語で返す（Bot検知、ログイン制限、DRM、403など）
- Cloudflareの画面でAPIのdetailメッセージを表示
- GET /health に yt-dlp バージョンを表示

GitHub:
1) render-api.zipを展開し、中の4ファイルを GitHub リポジトリ vydeo-api の render-api/ に上書きアップロードして Commit changes。
2) Renderで自動デプロイされるのを待つ。Dockerfileの変更が反映されない場合は Manual Deploy > Clear build cache & deploy。

Cloudflare:
3) cloudflare-pages.zipを Cloudflare Pages に再アップロード。通常は既存プロジェクトの Deployments > Create deployment にアップロードする。
4) ページを再読み込み（Ctrl+F5）、API URL https://vydeo-api.onrender.com を設定して再試行。

重要：最新版への更新だけでYouTubeのBot検知を回避できる保証はありません。
