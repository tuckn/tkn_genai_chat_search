# Changelog

利用者から見た変更を記録します。
バージョンは `pyproject.toml` と `tkn-genai-chat-search --version` で一致します。

## [Unreleased]

最初の版（0.1.0）として公開する予定の内容です。

### Added

- `list`：タイトル・session ID・作業フォルダ・日付で会話を特定し、1行1項目の `key=value` で出す。
- `search`：発言の本文を照合し、一致した発言と前後を会話の順に Markdown で引用する。ファイルと行番号、日時、ID を残す。
- `show`：指定した session の発言をすべて省略せずに出す。見つからない ID は終了コード1で知らせる。
- `sources`：読んだフォルダ、形式ごとのファイル数、読めなかった件数、タイトルを読んだ場所を出す。
- `config list`・`config init`：設定の確認と、編集済みのファイルを上書きしない初期化。
- すべてのコマンドで `--json`。出力の契約は `schema_version: 1`。
- Codex：`sessions`・`archived_sessions` の `.jsonl` と `.jsonl.zst` を読む。タイトルを `state_<n>.sqlite`（読み取り専用）と `session_index.jsonl` から読む。subagent と承認・guardian レビューを種類として区別する。
- Claude Code：`projects` の会話と subagent、`custom-title`・`ai-title`（改名前を含む）を読む。ユーザー発言の `<system-reminder>` を取り除く。
- 既定は、最後の発言が新しい順に10件。subagent とレビューは既定で除外する。
- ライブラリ `tkn_genai_chat_search.library`：アーカイブなどから読んだバイト列を、検索と同じ読み取りで `Session` にする。`detail=True` でツール実行（Claude Code の `tool_use`/`tool_result`、Codex の呼び出しと出力を `call_id` で組にしたもの、`item_completed`）と、Claude Code の発言の書き手（`turnOrigin` など）も返す。検索の出力は変えない。

### 移行（`tkn-codex-plugins` の検索scriptからの違い）

- Codex 検索は、圧縮された `.jsonl.zst` も読むようになった。旧scriptの既定（作成日の古い順・無制限）は引き継がず、`--sort-by created --order asc --limit 0` で同じ並びにできる。
- Codex の subagent の会話は既定で除外する。含めるには `--include-subagents` を付ける。
- JSON は旧scriptのどちらの版（Codex 2、Claude Code 1）とも別の契約になった。会話の ID は `session_id`、タイトルは `title` と `titles[]`、発言の UUID は `message_id`。
- 旧scriptの `--scope`、`--projects-root`、`--session-file`、`--thread-id`、`--format`、`--output` はない。読むフォルダは `--root SOURCE=PATH`、特定の会話は `show SESSION_ID`、JSON は `--json` を使う。
