# 読み取るログの形式

各アプリのログのうち、どのファイルのどの記録を読み、何を除外するかを記載します。
ログの形式は各アプリの内部仕様で、公開された安定した仕様ではありません。
このCLIが対応している範囲を示すもので、すべての版のアプリを読めることは保証しません。

## 1. 共通

- ファイルは読み取り専用で開きます。ロックは取りません。
- 文字コードは UTF-8（BOM 付きも可）です。`.jsonl.zst` は zstd を展開しながら読みます（複数フレームにも対応）。
- 1行が JSON として読めない、またはオブジェクトでない行は飛ばし、`parser_warnings` に数えます。行の中身はログに出しません。
- ファイル全体が読めない（開けない、文字コード・圧縮が壊れている）場合は、そのファイルの発言を一部だけ返すことはせず、`unreadable_files` に数えます。
- 同じ実体のファイルは、フォルダの指定が重なっても1回だけ読みます。別のファイルは、同じ session ID でも別の結果として扱います。

## 2. Codex

### 2.1. 探す場所

`<codex-home>`（既定は `CODEX_HOME`、なければ `~/.codex`）の次のフォルダを、下の階層まで探します。

```text
sessions/YYYY/MM/DD/rollout-<日時>-<thread-id>.jsonl
sessions/YYYY/MM/DD/rollout-<日時>-<thread-id>.jsonl.zst
sessions/YYYY/MM/DD/rollout-<日時>-<thread-id>_<segment-id>.jsonl.zst
archived_sessions/...
```

- `.jsonl` と `.jsonl.zst` を読みます。Codex は古い会話を `.jsonl.zst` に圧縮します。
- 名前に `.jsonl.` を含む他の形式（例：`.jsonl.gz`）は読まず、`unsupported_files` に数えます。
- `archived_sessions` の会話は `details.archive_status` が `archived` になります。
- `<thread-id>_<segment-id>` は、同じ thread の続きが別のファイルに書かれたものです。中身は最初の発言から重なることがあるため、ファイルごとに別の結果とし、`details.rollout_segment` に segment ID を入れます。
- ファイル名から thread ID が分かり、`--session-id`（subagent を含めない場合）や `--title` に合わないと分かるファイルは読みません（`files_skipped_by_filter`）。名前から ID が分からないファイルは必ず読みます。

### 2.2. 読む記録

| 記録 | 使うもの |
| --- | --- |
| 最初の `session_meta` | thread ID（`id`）、開始日時、作業フォルダ、`source`、`thread_source`、`parent_thread_id`、`originator`、`git.repository_url`。fork された会話では後に親の `session_meta` が続くため、最初のものだけを使います |
| `turn_context` | 以降の発言の turn ID と作業フォルダ |
| `response_item`（`type: message`、`role: user\|assistant`） | 本文、message ID（`id`）、`phase`、turn ID（`internal_chat_message_metadata_passthrough.turn_id`） |
| `event_msg`（`user_message`、`agent_message`） | 本文。同じ turn・同じ本文の発言がすでにあれば重複として捨てます |
| 古い形式の先頭オブジェクトと `message` | 古いログの会話情報と本文 |

本文は文字列か、`text`・`input_text` を持つ要素の配列から取り出します。
ユーザー発言から、Codex が付け足す AGENTS.md の指示・`<INSTRUCTIONS>`・`<environment_context>` を取り除き、`## My request for Codex:` があればその後だけを使います。
`developer` の発言、reasoning、`world_state` などは読みません。ツールの呼び出しと結果は、検索では読みません（ライブラリで `detail=True` のときだけ読みます。5節）。

### 2.3. 会話の種類

| 種類 | 条件 |
| --- | --- |
| `review` | `thread_source` が `approval_review` か `guardian_review`、`source.subagent.other` が `guardian`、またはユーザー発言が「The following is the Codex agent history」で始まる |
| `subagent` | 上に当たらず、`thread_source` が `subagent` か、`source` に `subagent` がある |
| `main` | それ以外 |

親は `parent_thread_id`、なければ `source.subagent.thread_spawn.parent_thread_id` です。

### 2.4. タイトル

タイトルはログの外にあります。どちらも読み取り専用で開き、なくても検索はできます。

| 場所 | 読むもの | `titles[].kind` |
| --- | --- | --- |
| `session_index.jsonl` | `id`、`thread_name`（同じ ID の行が複数あれば、値ごとに最後の行） | `thread-name` |
| `state_<n>.sqlite` の `threads` 表（`<n>` が最も大きいもの） | `id`、`name`、`title` | `name` は `thread-name`、`title` は `thread-title` |

- SQLite は `mode=ro` で開き、`id` と `name`・`title` のどちらかの列があることを確かめてから読みます。表や列がなければ `unsupported` として警告し、`session_index.jsonl` だけを使います。
- 表示するタイトル（`title`）は、`state_<n>.sqlite` の `name`、`session_index.jsonl` の `thread_name`、`threads.title` の順に選びます。
- `threads.title` は最初の発言の書き出しと同じことが多く、長い場合があります。`--title` はこれも照合します。

## 3. Claude Code

### 3.1. 探す場所

`<claude-config>`（既定は `CLAUDE_CONFIG_DIR`、なければ `~/.claude`）の `projects` の下を探します。

```text
projects/<encoded-project>/<session-id>.jsonl
projects/<encoded-project>/agent-<agent-id>.jsonl                      （古い形式の subagent）
projects/<encoded-project>/<session-id>/subagents/agent-<agent-id>.jsonl
```

- `memory/`、`tool-results/`、`mcp-tasks/`、`projects` 直下のファイルは読みません。
- `.orphaned-` を含む、退避された会話は読まず、`excluded_files.set_aside` に数えます。
- フォルダ名から作業フォルダを推測しません（変換で情報が失われるため）。記録された `cwd` を使います。

### 3.2. 読む記録

| 記録 | 使うもの |
| --- | --- |
| `type: user\|assistant` で `message` を持つもの | 本文（文字列、または `type: text` の要素）、`timestamp`、`cwd`、`uuid`、`parentUuid`、`sessionId`、`isSidechain`、`agentId` |
| `custom-title`（`customTitle`）、`ai-title`（`aiTitle`） | ファイルの session ID と `sessionId` が同じときだけタイトルにします。同じ値は最後の行で1回だけ残し、最後の値が今のタイトルです |
| `isMeta`・`isCompactSummary` の発言 | 自動で作られた文脈として除外し、`details.excluded_generated_messages` に数えます |
| ユーザー発言の中の `<system-reminder>…</system-reminder>` | Claude Code が付け足した文脈として取り除きます。それだけの発言は除外して同じく数えます |
| その他の記録（system、summary、progress など） | 読まずに種類ごとに `details.ignored_record_types` に数えます |

- `tool_use`・`tool_result`・`thinking`・画像の要素は、中に `text` があっても本文にしません（ツールの要素は、ライブラリで `detail=True` のときだけ読みます。5節）。
- `(role, uuid, 本文)` が完全に同じ発言は1回だけ残します（`details.duplicate_messages`）。UUID がない発言は重複を判定しません。
- 表示の順はファイルの行の順です。`parentUuid` をたどって分岐を選び直すことはしません。

### 3.3. 会話の種類と sidechain

- `subagents/` の下と `agent-*.jsonl` のファイル、またはすべての発言が sidechain のファイルは `subagent` です。親は `subagents/` の上のフォルダ名、古い形式では最初の記録の `sessionId` です。
- 通常の会話の中の `isSidechain` の発言は、`--include-subagents` がなければ除外します（`excluded_sidechain_messages`）。
- 別の会話から引き継いだ発言は、記録された `sessionId` を `messages[].session_id` に残します。`--session-id` はファイルの session ID と親の ID だけに一致します。

## 5. ライブラリとして読む（ツール実行と発言の書き手）

アーカイブなど、アプリのフォルダ以外から1つのログを読むツールは、`tkn_genai_chat_search.library` の関数を使います。検索と同じ読み取りの処理を使い、ファイルを開きません。`path` はバイト列の名前（アーカイブの相対パスなど）で、session ID と種類をファイル名と同じ規則で決めます。

| 関数 | 内容 |
| --- | --- |
| `read_claude_code(data, path, detail=True)` | Claude Code の会話1つ。`LogRead(session, coverage, error)` を返す |
| `read_codex(data, path, titles=None, detail=True)` | Codex の rollout 1つ（`.jsonl.zst` は先頭のマジックナンバーで展開） |
| `read_codex_titles(data, path)` | `session_index.jsonl` のバイト列からタイトル。読めない行があれば `TitleStore.status` が `partial` で、`detail` に件数 |
| `claude_code_transcript_kind(path)`・`codex_rollout_status(path)` | 相対パスが会話のファイルか（`main`・`subagent`／`active`・`archived`）。古い形式の `agent-*.jsonl` は `subagent` |

`LogRead.coverage` は、フォルダを読むときと同じ件数（`files_discovered`・`files_read`・`sessions_read` など）を、1つのログについて数えたものです。テキストの発言がなくても、`detail=True` でツール実行があれば `Session` を返します。

`detail=True` のとき、`Session` に次が入ります。検索の出力（`summary()`・`messages[]`）は変わりません。

- `tool_events`（`ToolEvent`）：ツール実行。`call_id`・`name`・`arguments`・`timestamp`・`line_number`・`turn_id`・`cwd`・`status`・`is_error`・`result_line_number`・`output_chars`・`error_preview`（失敗時の先頭200文字）。
  - Claude Code：assistant の `tool_use` と、user の `tool_result` を `id` と `tool_use_id` で組にします。`is_error` は `tool_result.is_error`。結果のない実行は `result_line_number` が `None` です。
  - Codex：`function_call`・`custom_tool_call`・`local_shell_call`・`web_search_call`・`tool_search_call` と、その `*_output` を `call_id` で組にします（`origin: call`）。出力の `metadata.exit_code` か `Exit code: n` の行があれば `is_error` にします。`event_msg` の `exec_command_end`・`patch_apply_end` も結果に反映します。`event_msg` の `item_completed`（`CommandExecution`・`FileChange`・`McpToolCall`・`WebSearch` など）は、それだけで1回の実行として扱います（`origin: item`。`exit_code`・`status` から `is_error`）。会話・reasoning・モードの切り替えの item は除きます。
- `message_attributes`：Claude Code のユーザー発言の行ごとに、書き手を示す項目（`turnOrigin`・`promptSource`・`entrypoint`・`userType`・`isSidechain`）。別のセッションから渡された依頼文は `turnOrigin` が `human` 以外になります。

## 4. 実データでの確認

| 項目 | 内容 |
| --- | --- |
| 実施日 | 2026-11-03 |
| 対象 | 初回 commit 前の作業ツリー（0.1.0）、Windows 11、Python 3.12 |
| 方法 | 作者のPCのログに対して `sources`・`list --title`・`search --query`・`show` を実行し、`tkn-codex-plugins` の旧scriptの結果と比べた |

確認した結果：

- Codex：1,381ファイル（`.jsonl` 85、`.jsonl.zst` 1,296）をすべて読み、読めないファイルと壊れた行は0でした。旧scriptは `.jsonl` だけを読むため、85ファイルしか対象にしていませんでした。
- Codex のタイトル：`session_index.jsonl` と `state_5.sqlite`（列 `id`・`name`・`title`）から読み、thread name で会話を特定できました。
- 旧scriptとの比較：`plugin`・`設計`・`zstd` の3語で、旧scriptが見つけた会話はすべて新しいCLIでも見つかりました。Claude Code は同じ件数、Codex は圧縮ファイルの分だけ増えました（例：`plugin` で4件から128件）。
- fork された Codex の会話には2つ目の `session_meta`（親のもの）があり、最初のものを使う必要があることを確認しました。
- Claude Code のユーザー発言229件のうち54件に `<system-reminder>` があり、39件はそれだけの発言でした。

未確認：Linux・macOS での動作、Codex・Claude Code の他の版のログ、別のアカウント・PCのログ。
