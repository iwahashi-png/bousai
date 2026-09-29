# 災害通報機能の設定

## 本番（Vercel）

このアプリはVercel上の一時ファイルシステムに通報や写真を保存しません。本番ではSupabaseのPostgresとStorageを使います。

1. SupabaseプロジェクトのSQL Editorで [`supabase/community_reports.sql`](supabase/community_reports.sql) を実行します。通報テーブルはRLSを有効にし、写真用の公開バケットはPNGのみ・8MB上限で作成します。
2. VercelのProject Settings > Environment Variablesに次を設定し、再デプロイします。

| 変数 | 用途 |
| --- | --- |
| `SUPABASE_URL` | SupabaseプロジェクトURL |
| `SUPABASE_SERVICE_ROLE_KEY` | サーバー専用Service Roleキー。ブラウザーへ返したり、ログへ出したりしない |
| `FLASK_SECRET_KEY` | 推測困難なランダムセッション署名鍵 |
| `STAFF_USERNAME` | 職員ログイン名 |
| `STAFF_PASSWORD` | 職員ログインパスワード |

任意で `SUPABASE_REPORTS_TABLE`（既定値 `community_reports`）と `SUPABASE_REPORT_PHOTO_BUCKET`（既定値 `community-report-photos`）を変更できます。画像はJPG/JPEG・PNG・WebPを受け付け、安全にPNGへ再エンコードしてから8MB上限でアップロードします。

SQL適用前やSupabase設定不足時に通報を書き込まず、設定エラーを画面へ表示します。Service Roleキーはサーバー環境変数だけに置いてください。

## ローカル開発・テスト

Vercel以外ではSQLiteとローカル画像ディレクトリを使用します。既定ではDBは `bousai_app/data/community_reports.sqlite3`、写真は `bousai_app/uploads/community-reports/` に保存されます。どちらもGit管理対象外です。

```sh
python -m pip install -r requirements.txt
STAFF_USERNAME=staff STAFF_PASSWORD='ローカル用パスワード' FLASK_SECRET_KEY='ローカル用ランダム鍵' python app.py
```

職員ログインと `/staff/community-reports` を使う場合も、環境変数の設定が必要です。既存の避難所・発信データ形式は変更しません。