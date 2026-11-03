"""Command line interface: argument parsing, dispatch and exit codes."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .config import SOURCE_KEYS, ConfigError, ResolvedConfig, init_config, load_config, user_config_path
from .logging_utils import configure_logging, get_logger, log_success
from .normalize import parse_utc_offset
from .readers import READERS, Root
from .render import coverage_by_source, list_lines, search_markdown, to_json, to_lines
from .search import Filters, SearchError, prepare, report, scan

PROG = "tkn-genai-chat-search"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
LOGGER = get_logger()

DESCRIPTION = """\
Search local Codex and Claude Code chat logs and print quoted evidence.

Read-only: it never changes, moves or indexes the logs, starts no application and sends nothing
over the network. Only `config init` writes, and only its own configuration file.
"""

EPILOG = """\
output:
  list, sources, config  key=value lines on stdout (one value per line)
  search, show           Markdown on stdout (quoted messages with their source line)
  --json                 one JSON document on stdout instead, for scripts and agents
  Progress, warnings and errors go to stderr as "[LEVEL] message".

exit codes:
  0  success, including a search with no matches
  1  failure: configuration error, no chat log folder found, or `show` could not find an ID
  2  invalid arguments
"""


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(f"{message} (see {self.prog} --help)")


def _nonnegative(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if result < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return result


def _offset(value: str) -> str:
    try:
        parse_utc_offset(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return value


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use a date such as 2026-11-01") from None


def _common() -> _Parser:
    parent = _Parser(add_help=False)
    parent.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help="read this configuration file after the others"
    )
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON document")
    verbosity = parent.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="print only errors on stderr"
    )
    verbosity.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="also print debug details on stderr, such as each malformed record",
    )
    return parent


def _add_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        action="append",
        choices=list(SOURCE_KEYS),
        help="read only this source (repeatable; default: every source)",
    )
    parser.add_argument(
        "--root",
        action="append",
        metavar="SOURCE=PATH",
        help="read this application folder instead of the default (repeatable), "
        "for example codex=C:\\path\\to\\.codex or claude-code=C:\\path\\to\\.claude",
    )


def _add_kinds(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-subagents",
        action="store_true",
        help="also return subagent sessions (Codex and Claude Code) and Claude Code sidechain messages",
    )
    parser.add_argument(
        "--include-approval-reviews", action="store_true", help="also return Codex approval and guardian reviews"
    )


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--title",
        action="append",
        default=[],
        help="part of a recorded session title, including earlier titles; ignores case, full/half width "
        "and spaces; repeated values must all match",
    )
    parser.add_argument(
        "--session-id", action="append", default=[], help="a session ID, or the parent ID of a subagent (repeatable)"
    )
    parser.add_argument(
        "--cwd-contains", action="append", default=[], help="part of the recorded working folder (repeatable: AND)"
    )
    parser.add_argument(
        "--cwd-field",
        choices=["session", "message"],
        default="session",
        help="session: the folder where the session started (default); message: each message's folder, "
        "keeping only messages in that folder",
    )
    parser.add_argument("--date-from", type=_day, help="first day to include (YYYY-MM-DD, inclusive)")
    parser.add_argument("--date-to", type=_day, help="last day to include (YYYY-MM-DD, inclusive)")
    parser.add_argument(
        "--date-field",
        choices=["created", "updated", "message"],
        default="created",
        help="date compared with --date-from/--date-to: created (first message, default), updated (last "
        "message), or message (keep only messages on those days)",
    )
    parser.add_argument(
        "--utc-offset",
        type=_offset,
        help='offset for day boundaries, e.g. --utc-offset=+09:00 (default: config, "+00:00")',
    )
    _add_kinds(parser)
    parser.add_argument("--sort-by", choices=["created", "updated"], help="default: config, updated")
    parser.add_argument("--order", choices=["asc", "desc"], help="default: config, desc")
    parser.add_argument(
        "--limit", type=_nonnegative, help="sessions to return after sorting; 0 = all (default: config, 10)"
    )


def _add_evidence(parser: argparse.ArgumentParser, *, full: bool) -> None:
    default = "0" if full else "config, 8"
    parser.add_argument(
        "--messages-per-role",
        type=_nonnegative,
        help=f"soft number of messages per role; matches always stay; 0 = all (default: {default})",
    )
    if not full:
        parser.add_argument(
            "--context-messages", type=_nonnegative, help="messages kept around each match (default: config, 1)"
        )
    parser.add_argument(
        "--max-message-chars",
        type=_nonnegative,
        help=f"characters per excerpt; 0 = no shortening (default: {'0' if full else 'config, 1200'})",
    )
    parser.add_argument(
        "--include-assistant",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show assistant messages; --no-include-assistant hides them without changing matching",
    )


def build_parser() -> _Parser:
    common = _common()
    parser = _Parser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", parser_class=_Parser, metavar="COMMAND")

    listing = commands.add_parser(
        "list",
        parents=[common],
        help="one entry per session: source, ID, title, folder and dates (no message text)",
        description="List sessions without their message text. Use --title to find a session by its title.",
    )
    _add_scope(listing)
    _add_filters(listing)

    finder = commands.add_parser(
        "search",
        parents=[common],
        help="match message text and print the matching messages with context",
        description="Match user and assistant message text (not titles, tool output or metadata) and print "
        "each matching message with nearby messages, in conversation order.",
    )
    _add_scope(finder)
    finder.add_argument(
        "--query", action="append", default=[], help="text to find; repeated queries must all occur in a session"
    )
    finder.add_argument("--regex", action="store_true", help="treat --query as a Python regular expression")
    finder.add_argument("--query-user-only", action="store_true", help="match only user messages")
    _add_filters(finder)
    _add_evidence(finder, full=False)

    shower = commands.add_parser(
        "show",
        parents=[common],
        help="print every message of the given sessions",
        description="Print all message text of each session ID, without shortening by default.",
    )
    shower.add_argument("session_ids", nargs="+", metavar="SESSION_ID")
    _add_scope(shower)
    _add_kinds(shower)
    _add_evidence(shower, full=True)

    sources = commands.add_parser(
        "sources",
        parents=[common],
        help="where logs were found, file counts per format, and files that could not be read",
        description="Read every log once and report coverage: folders, formats, unreadable files and title stores.",
    )
    _add_scope(sources)

    config = commands.add_parser("config", parents=[common], help="show or create the configuration")
    config_commands = config.add_subparsers(dest="config_command", parser_class=_Parser, metavar="ACTION")
    config_commands.add_parser(
        "list", parents=[common], help="print the resolved settings and the files they came from (read-only)"
    )
    init = config_commands.add_parser(
        "init",
        parents=[common],
        help=f"create {user_config_path()} from the example (never replaces an edited file without --force)",
    )
    init.add_argument("--force", action="store_true", help="back up an edited file, then replace it")
    init.add_argument("--dry-run", action="store_true", help="report what would happen without writing")
    return parser


def _option(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


def _overrides(args: argparse.Namespace, *, full: bool) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    roots: dict[str, list[str]] = {}
    for item in _option(args, "root") or []:
        source, separator, path = item.partition("=")
        if not separator or source not in SOURCE_KEYS or not path.strip():
            raise UsageError(f"--root must look like SOURCE=PATH with SOURCE one of {', '.join(SOURCE_KEYS)}: {item}")
        roots.setdefault(SOURCE_KEYS[source], []).append(path.strip())
    if roots:
        overrides["roots"] = roots
    defaults = {}
    names = ["limit", "sort_by", "order", "utc_offset", "context_messages"]
    if not full:
        names += ["messages_per_role", "max_message_chars"]
    for name in names:
        value = _option(args, name)
        if value is not None:
            defaults[name] = value
    if defaults:
        overrides["defaults"] = defaults
    return overrides


def _plan(config: ResolvedConfig, selected: list[str], *, require_logs: bool) -> list[tuple[str, Any, list[Root]]]:
    plan = []
    missing: list[Root] = []
    for source in selected:
        reader = READERS[source]
        roots, explicit = config.roots(source, reader.default_roots())
        if explicit:
            missing += [root for root in roots if not root.path.is_dir()]
        plan.append((source, reader, roots))
    # A mistyped folder must not look like a search with no matches.
    if missing:
        message = "folder not found: " + "; ".join(f"{root.path} ({root.origin})" for root in missing)
        if any(root.origin == "option" for root in missing):
            raise UsageError(message)
        raise ConfigError(message)
    if require_logs and not any(root.path.is_dir() for _source, _reader, roots in plan for root in roots):
        looked = "; ".join(f"{source}: {root.path}" for source, _reader, roots in plan for root in roots)
        raise ConfigError(f"no chat log folder found ({looked}); pass --root or set roots in the configuration")
    return plan


def _summarize(document: dict[str, Any]) -> None:
    coverage = document["coverage"]
    parts = [f"{item['files_read']} {item['source']} files" for item in coverage["sources"]]
    LOGGER.info(
        "Read %s; %d sessions matched, %d returned",
        " and ".join(parts) or "no files",
        coverage["matched_sessions"],
        coverage["returned_sessions"],
    )
    for item in coverage["sources"]:
        if item["parser_warnings"]:
            LOGGER.warning(
                "%s: skipped %d malformed records in %d files; run with --verbose to see each path and line",
                item["source"],
                item["parser_warnings"],
                item["files_with_parser_warnings"],
            )


def _search_command(args: argparse.Namespace, command: str) -> tuple[dict[str, Any], str, int]:
    full = command == "show"
    config = load_config(_option(args, "config"), overrides=_overrides(args, full=full))
    defaults = config.defaults
    selected = list(dict.fromkeys(_option(args, "source") or list(SOURCE_KEYS)))
    plan = _plan(config, selected, require_logs=command != "sources")
    if command == "show":
        filters = Filters(
            sources=tuple(selected),
            session_ids=tuple(args.session_ids),
            include_subagents=args.include_subagents,
            include_approval_reviews=args.include_approval_reviews,
            include_assistant=args.include_assistant,
            sort_by="created",
            order="asc",
            limit=0,
            messages_per_role=_option(args, "messages_per_role") or 0,
            max_message_chars=_option(args, "max_message_chars") or 0,
            utc_offset=parse_utc_offset(defaults["utc_offset"]),
            exact_ids=True,
        )
    elif command == "sources":
        filters = Filters(sources=tuple(selected), limit=0, utc_offset=parse_utc_offset(defaults["utc_offset"]))
    else:
        filters = Filters(
            sources=tuple(selected),
            queries=tuple(_option(args, "query") or ()),
            regex=bool(_option(args, "regex")),
            query_user_only=bool(_option(args, "query_user_only")),
            include_assistant=_option(args, "include_assistant", True),
            titles=tuple(args.title),
            session_ids=tuple(args.session_id),
            cwd_contains=tuple(args.cwd_contains),
            cwd_field=args.cwd_field,
            date_from=args.date_from,
            date_to=args.date_to,
            date_field=args.date_field,
            utc_offset=parse_utc_offset(defaults["utc_offset"]),
            include_subagents=args.include_subagents,
            include_approval_reviews=args.include_approval_reviews,
            sort_by=defaults["sort_by"],
            order=defaults["order"],
            limit=defaults["limit"],
            messages_per_role=defaults["messages_per_role"],
            context_messages=defaults["context_messages"],
            max_message_chars=defaults["max_message_chars"],
        )
    try:
        prepared = prepare(filters)
    except SearchError as exc:
        raise UsageError(str(exc)) from None
    result = scan(plan, prepared)
    document = report(command, prepared, result, with_messages=command in {"search", "show"})
    _summarize(document)
    code = EXIT_OK
    if command == "show":
        missing = [value for value in args.session_ids if value.strip().casefold() not in result.found_ids]
        document["missing_session_ids"] = missing
        if missing:
            document["status"] = "not_found" if not result.matches else "partial"
            LOGGER.error("session not found: %s; find IDs with `list --title` or `search --query`", ", ".join(missing))
            code = EXIT_FAILED
    if _option(args, "json", False):
        return document, to_json(document), code
    if command == "list":
        return document, list_lines(document), code
    if command == "sources":
        coverage = document["coverage"]
        text = to_lines({"command": "sources", "status": "success", **coverage_by_source(coverage)})
        return document, text, code
    heading = "Chat Session" if command == "show" else "Chat Search Results"
    return document, search_markdown(document, heading=heading), code


def _config_command(args: argparse.Namespace) -> tuple[dict[str, Any], str, int]:
    action = _option(args, "config_command")
    if action is None:
        raise UsageError(f"choose a config action: list or init (see {PROG} config --help)")
    if action == "init":
        result = init_config(force=args.force, dry_run=args.dry_run)
        document = {"schema_version": 1, "command": "config init", **result}
        status = result["status"]
        if status in {"created", "replaced"}:
            log_success(LOGGER, "Configuration %s: %s", status, result["path"])
        elif status == "preserved":
            LOGGER.warning(
                "An edited configuration already exists and was kept: %s; add --force to back it up and replace it",
                result["path"],
            )
        else:
            LOGGER.info("Configuration %s: %s", status.replace("_", " "), result["path"])
    else:
        LOGGER.info("Showing resolved configuration")
        config = load_config(_option(args, "config"))
        resolved_roots = {}
        for source, reader in READERS.items():
            roots, _explicit = config.roots(source, reader.default_roots())
            resolved_roots[source] = [
                {"path": str(root.path.absolute()), "exists": root.path.is_dir(), "origin": root.origin}
                for root in roots
            ]
        document = {
            "schema_version": 1,
            "command": "config list",
            "status": "success",
            "user_config_path": str(user_config_path()),
            **config.to_dict(),
            "resolved_roots": resolved_roots,
        }
    text = to_json(document) if _option(args, "json", False) else to_lines(document)
    return document, text, EXIT_OK


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], str, int]:
    command = _option(args, "command")
    if command is None:
        raise UsageError(f"choose a command: list, search, show, sources or config (see {PROG} --help)")
    if command == "config":
        return _config_command(args)
    return _search_command(args, command)


def _failure(command: str | None, code: int, message: str, *, as_json: bool, path: str | None = None) -> str:
    document: dict[str, Any] = {
        "schema_version": 1,
        "command": command,
        "status": "failed",
        "exit_code": code,
        "error": message,
    }
    if path:
        document["error_path"] = path
    return to_json(document) if as_json else to_lines(document)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    as_json = "--json" in raw
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except UsageError as exc:
        configure_logging()
        LOGGER.error("%s", exc)
        sys.stdout.write(_failure(None, EXIT_USAGE, str(exc), as_json=as_json))
        return EXIT_USAGE
    configure_logging(quiet=bool(_option(args, "quiet")), verbose=bool(_option(args, "verbose")))
    command = _option(args, "command")
    if command == "config" and _option(args, "config_command"):
        command = f"config {args.config_command}"
    try:
        _document, text, code = _dispatch(args)
    except UsageError as exc:
        LOGGER.error("%s", exc)
        sys.stdout.write(_failure(command, EXIT_USAGE, str(exc), as_json=as_json))
        return EXIT_USAGE
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        sys.stdout.write(_failure(command, EXIT_FAILED, str(exc), as_json=as_json, path=exc.path))
        return EXIT_FAILED
    except KeyboardInterrupt:
        LOGGER.error("interrupted")
        return EXIT_INTERRUPTED
    except OSError as exc:
        LOGGER.error("%s: %s", type(exc).__name__, exc)
        sys.stdout.write(_failure(command, EXIT_FAILED, f"{type(exc).__name__}: {exc}", as_json=as_json))
        return EXIT_FAILED
    sys.stdout.write(text)
    return code
