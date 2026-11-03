# 出力の契約

`tkn-genai-chat-search` の標準出力・標準エラー・終了コードと、`--json` の項目を定めます。
スクリプトや生成AIから結果を解析するときは `--json` を使い、人向けの `key=value` や Markdown の文字列を解析しないでください。

## 1. 共通の約束

- 標準出力には結果だけを出します。進捗・警告・エラーは標準エラーに `[LEVEL] message` の形で出します。
- `--json` を付けると、どのコマンドも、成功・失敗にかかわらず1つの JSON を出します。
- 出力の形式は `--json` の有無だけで決まり、端末かパイプかでは変わりません。
- `-q`・`-v` は標準エラーの量だけを変え、標準出力の内容は変えません。
- 文字コードは UTF-8 です。

| 終了コード | 意味 | `status` |
| --- | --- | --- |
| 0 | 成功（検索結果0件を含む） | `success`、`config init` では `created` などの結果 |
| 1 | 失敗（設定の誤り、ログのフォルダが1つもない、`show` の ID が見つからない） | `failed`、`show` では `not_found` か `partial` |
| 2 | 引数の誤り | `failed` |

## 2. 失敗したとき

```json
{
  "schema_version": 1,
  "command": "search",
  "status": "failed",
  "exit_code": 2,
  "error": "folder not found: C:\\path\\to\\.codex (option)"
}
```

設定ファイルの誤りでは、そのファイルのパスを `error_path` に入れます。
`--json` がない場合は、同じ項目を `key=value` で出します。

## 3. `list`・`search`・`show`・`sources`

```json
{
  "schema_version": 1,
  "command": "search",
  "status": "success",
  "filters": {},
  "coverage": {"matched_sessions": 0, "returned_sessions": 0, "sources": []},
  "sessions": []
}
```

- `schema_version`：この JSON の版です。項目の追加では上げず、既存の項目の意味や型を変えるときに上げます。
- `filters`：解決後の条件です（`sources`、`queries`、`titles`、`session_ids`、`cwd_contains`、`cwd_field`、`date_from`、`date_to`、`date_field`、`utc_offset`、`include_subagents`、`include_approval_reviews`、`include_assistant`、`regex`、`query_user_only`、`sort_by`、`order`、`limit`、`messages_per_role`、`context_messages`、`max_message_chars`）。
- `coverage.matched_sessions`：条件に合った会話の数（`limit` を適用する前）。`returned_sessions` は返した数です。
- `show` は `missing_session_ids`（見つからなかった ID）も持ちます。
- `sources` の `sessions` は、条件なしで読めたすべての会話です。人向けの出力では件数だけを表示します。

### 3.1. `coverage.sources[]`（アプリごとの検索範囲）

| 項目 | 意味 |
| --- | --- |
| `source` | `codex` か `claude-code` |
| `roots[]` | 読んだフォルダ。`path`、`exists`、`origin`（`default`、`default ($CODEX_HOME)`、`config:user`、`option` など） |
| `files_discovered` | 見つけたログファイルの数。`files_by_format` は形式ごとの内訳（`jsonl`、`jsonl.zst`） |
| `files_read` | 実際に読んだ数 |
| `files_skipped_by_filter` | ファイル名から `--session-id`・`--title` に合わないと分かり、読まなかった数 |
| `unreadable_files` | 開けない・文字コードや圧縮が壊れていて読めなかった数。その会話は結果に含めません |
| `unreadable_directories` | 中を見られなかったフォルダの数 |
| `unsupported_files` | 未対応の形式のログ（例：`.jsonl.gz`）の数 |
| `files_without_sessions` | 会話の情報（Codex の `session_meta`）がなかった数 |
| `files_without_messages` | 会話の本文が1つもなかった数 |
| `parser_warnings`、`files_with_parser_warnings` | 壊れていて飛ばした行の数と、そのファイルの数 |
| `sessions_read`、`sessions_by_kind` | 読めた会話の数と、種類（`main`、`subagent`、`review`）ごとの内訳 |
| `excluded_sessions` | 種類のために除外した数（`--include-subagents`・`--include-approval-reviews` で含められます） |
| `excluded_files` | 読まずに除外したファイルの数（Claude Code の `set_aside`） |
| `excluded_sidechain_messages` | 通常の会話から除外した Claude Code の sidechain の発言の数 |
| `title_stores[]` | タイトルを読んだ場所。`path`、`status`（`read`、`missing`、`unreadable`、`unsupported`）、`titles`（読んだ数）、`detail` |
| `details` | アプリ固有の件数。Claude Code は `excluded_generated_messages`、`duplicate_messages`、`non_text_messages`、`ignored_record_types` |
| `not_searched` | このアプリについて検索しなかったものの説明 |

この件数は今回読んだ範囲のもので、削除された履歴や保存されなかった会話は数えられません。

### 3.2. `sessions[]`

| 項目 | 意味 |
| --- | --- |
| `source` | `codex` か `claude-code` |
| `session_id` | Codex は thread ID、Claude Code はログのファイル名（`.jsonl` の前） |
| `kind` | `main`、`subagent`、`review`（Codex の承認・guardian レビュー） |
| `parent_session_id` | subagent・レビューの親。なければ空文字 |
| `title` | アプリが今表示するタイトル。Claude Code は手動の名前、なければ AI のタイトル。Codex は thread name、なければ保存された title |
| `titles[]` | 記録されたすべてのタイトル。`kind`、`title`、`source_path`、`line_number`（SQLite では `null`） |
| `matched_titles[]` | `--title` に一致したタイトル（同じ形） |
| `cwd` | 会話を始めた作業フォルダ（記録どおり） |
| `created`、`updated` | 最初と最後の発言の日時（Codex の `created` は会話の開始日時） |
| `paths[]` | 読んだログファイル |
| `message_count`、`eligible_message_count` | 会話の発言数と、日付・フォルダの条件を満たした発言数 |
| `details` | アプリ固有の情報。Codex は `archive_status`（`active`、`archived`）、`rollout_segment`、`originator`、`app_source`、`thread_source`、`repository_url`。Claude Code は `agent_id`、`recorded_session_ids` |

`list` では `first_user_message`（最初のユーザー発言の1行要約、160文字まで）を返します。
`search`・`show` では次も返します。

| 項目 | 意味 |
| --- | --- |
| `matched_queries` | 会話の中で一致した `--query` |
| `omitted_message_count` | 表示できたが件数の目安のために省いた発言の数 |
| `hidden_matching_message_count` | `--no-include-assistant` で隠した、一致した発言の数 |
| `messages[]` | 選んだ発言（会話の順） |

### 3.3. `messages[]`

| 項目 | 意味 |
| --- | --- |
| `role` | `user` か `assistant` |
| `text` | 表示する本文（長い場合は抜粋）。`text_start`・`text_end` は元の本文の中の位置、`truncated` は抜粋かどうか |
| `timestamp` | 記録された日時。なければ空文字 |
| `source_path`、`line_number` | ログファイルと、その記録の行番号（1から） |
| `message_id` | Claude Code の UUID、Codex の message ID |
| `parent_id` | Claude Code の `parentUuid` |
| `turn_id` | Codex の turn ID |
| `session_id` | Claude Code でその記録に書かれた session ID（引き継いだ会話では別の ID になることがあります） |
| `cwd` | その発言の作業フォルダ |
| `phase` | Codex のアシスタント発言の段階（例：`final_answer`） |
| `is_sidechain`、`agent_id` | Claude Code の sidechain・subagent の情報 |
| `matched_queries` | この発言で一致した `--query` |
| `matched_excerpts[]` | 一致ごとの抜粋。`query`、`text`、`text_start`、`text_end`、`truncated`、`match_start`、`match_end` |

位置は元の本文の文字の位置（0から、終わりは含まない）で、JSON のバイト位置ではありません。
記録にない値は推測せず、空文字のままにします。

## 4. `config list`

`config_sources[]`（`name`、`path`、`schema_version`、`migrated`）、`effective_schema_version`、`settings`（解決後の設定）、`value_sources`（各値を決めた場所：`built-in`、`config:user`、`config:project`、`config:explicit`、`option`）、`resolved_roots`（アプリごとの実際のフォルダ）、`user_config_path` を返します。

## 5. `config init`

`path`、`status`（`created`、`unchanged`、`preserved`、`replaced`、`--dry-run` では `would_create`、`would_replace`）、`backup_path`、`dry_run` を返します。
`preserved` は、編集済みのファイルを残したことを表します（終了コード0）。
