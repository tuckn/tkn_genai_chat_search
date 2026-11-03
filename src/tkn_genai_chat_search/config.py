"""Layered configuration: built-in defaults, user file, current-folder file, --config file, options."""

from __future__ import annotations

import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .normalize import UTC_OFFSET_PATTERN
from .readers import Root

SCHEMA_VERSION = "1.0.0"
SUPPORTED_MAJOR = 1
SUPPORTED_MAX_MINOR = 0
APPLICATION_ID = "genai_chat_search"
# Command line source name -> configuration key.
SOURCE_KEYS = {"codex": "codex", "claude-code": "claude_code"}
INTEGER_DEFAULTS = ("limit", "messages_per_role", "context_messages", "max_message_chars")
CHOICES = {"sort_by": ("created", "updated"), "order": ("asc", "desc")}

BUILT_IN: dict[str, Any] = {
    "roots": {"codex": None, "claude_code": None},
    "defaults": {
        "limit": 10,
        "sort_by": "updated",
        "order": "desc",
        "messages_per_role": 8,
        "context_messages": 1,
        "max_message_chars": 1200,
        "utc_offset": "+00:00",
    },
}
VERSION_PATTERN = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


class ConfigError(Exception):
    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


def user_config_path() -> Path:
    return Path.home() / ".tkn" / APPLICATION_ID / "config.yaml"


def project_config_path(cwd: Path) -> Path:
    return cwd / ".tkn" / "config.yaml"


def config_template() -> str:
    return files("tkn_genai_chat_search.resources").joinpath("config.example.yaml").read_text(encoding="utf-8")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key: {key}", key_node.start_mark)
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def check_version(value: Any, where: str) -> str:
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise ConfigError(f'{where}: schema_version must be a quoted "MAJOR.MINOR.PATCH" string', path=where)
    major, minor, _patch = (int(part) for part in value.split("."))
    if major != SUPPORTED_MAJOR or minor > SUPPORTED_MAX_MINOR:
        raise ConfigError(
            f"{where}: schema_version {value} is not supported; this version reads "
            f"{SUPPORTED_MAJOR}.0.x to {SUPPORTED_MAJOR}.{SUPPORTED_MAX_MINOR}.x. "
            "Update tkn-genai-chat-search or rewrite the file from `config init`",
            path=where,
        )
    return value


def validate_layer(data: Any, where: str, *, require_version: bool = True) -> dict[str, Any]:
    """Check one layer before it is merged; return it without `schema_version`."""
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: the configuration must be a mapping", path=where)
    layer = dict(data)
    if require_version:
        if "schema_version" not in layer:
            raise ConfigError(f'{where}: schema_version is required, for example schema_version: "{SCHEMA_VERSION}"')
        check_version(layer.pop("schema_version"), where)
    unknown = sorted(str(key) for key in set(layer) - set(BUILT_IN))
    if unknown:
        raise ConfigError(f"{where}: unknown keys: {', '.join(unknown)}", path=where)
    roots = layer.get("roots", {})
    if not isinstance(roots, dict):
        raise ConfigError(f"{where}: roots must be a mapping of source to a list of folders", path=where)
    unknown = sorted(str(key) for key in set(roots) - set(BUILT_IN["roots"]))
    if unknown:
        raise ConfigError(f"{where}: unknown roots keys: {', '.join(unknown)} (use codex, claude_code)", path=where)
    for key, value in roots.items():
        if value is not None and (
            not isinstance(value, list) or not value or not all(isinstance(p, str) and p.strip() for p in value)
        ):
            raise ConfigError(f"{where}: roots.{key} must be null or a non-empty list of folder paths", path=where)
    defaults = layer.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError(f"{where}: defaults must be a mapping", path=where)
    unknown = sorted(str(key) for key in set(defaults) - set(BUILT_IN["defaults"]))
    if unknown:
        raise ConfigError(f"{where}: unknown defaults keys: {', '.join(unknown)}", path=where)
    for key, value in defaults.items():
        if key in INTEGER_DEFAULTS and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ConfigError(f"{where}: defaults.{key} must be an integer of 0 or more", path=where)
        if key in CHOICES and value not in CHOICES[key]:
            raise ConfigError(f"{where}: defaults.{key} must be one of: {', '.join(CHOICES[key])}", path=where)
        if key == "utc_offset" and (not isinstance(value, str) or not UTC_OFFSET_PATTERN.fullmatch(value)):
            raise ConfigError(f'{where}: defaults.utc_offset must be a quoted offset such as "+09:00"', path=where)
    return layer


def read_layer(path: Path) -> dict[str, Any]:
    where = str(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"{where}: cannot read the configuration ({type(exc).__name__})", path=where) from None
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"{where}: invalid YAML: {exc}", path=where) from None
    if data is None:
        raise ConfigError(f'{where}: the file is empty; it needs at least schema_version: "{SCHEMA_VERSION}"')
    version = data.get("schema_version") if isinstance(data, dict) else None
    layer = validate_layer(data, where)
    layer["_schema_version"] = version
    return layer


@dataclass(frozen=True)
class ConfigSource:
    name: str
    path: str | None
    schema_version: str | None
    migrated: bool = False


@dataclass
class ResolvedConfig:
    settings: dict[str, Any]
    sources: list[ConfigSource]
    value_sources: dict[str, str]
    cwd: Path

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.settings["defaults"])

    def roots(self, source: str, default_roots: list[Root]) -> tuple[list[Root], bool]:
        """Folders for one source, and whether they were chosen explicitly (config or option)."""
        key = SOURCE_KEYS[source]
        configured = self.settings["roots"][key]
        if configured is None:
            return default_roots, False
        origin = self.value_sources.get(f"roots.{key}", "config")
        return [Root(self.resolve_path(value), origin) for value in configured], True

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.cwd / path

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_sources": [vars(source) for source in self.sources],
            "effective_schema_version": SCHEMA_VERSION,
            "settings": deepcopy(self.settings),
            "value_sources": dict(sorted(self.value_sources.items())),
        }


def _merge(settings: dict[str, Any], layer: dict[str, Any], name: str, winners: dict[str, str]) -> None:
    for section in ("roots", "defaults"):
        for key, value in layer.get(section, {}).items():
            settings[section][key] = deepcopy(value)
            winners[f"{section}.{key}"] = name


def load_config(
    explicit: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    cwd: Path | None = None,
    user_path: Path | None = None,
) -> ResolvedConfig:
    """Merge built-in defaults, the user file, ./.tkn/config.yaml, --config, then option overrides."""
    current = (cwd or Path.cwd()).absolute()
    settings = deepcopy(BUILT_IN)
    winners = {f"{section}.{key}": "built-in" for section in BUILT_IN for key in BUILT_IN[section]}
    sources = [ConfigSource("built-in", None, SCHEMA_VERSION)]
    layers: list[tuple[str, Path, bool]] = [
        ("user", user_path or user_config_path(), False),
        ("project", project_config_path(current), False),
    ]
    if explicit is not None:
        layers.append(("explicit", explicit if explicit.is_absolute() else current / explicit, True))
    seen: set[Path] = set()
    for name, path, required in layers:
        if not path.is_file():
            if required:
                raise ConfigError(f"configuration file not found: {path}", path=str(path))
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        layer = read_layer(path)
        sources.append(ConfigSource(name, str(path), layer.pop("_schema_version")))
        _merge(settings, layer, f"config:{name}", winners)
    if overrides:
        layer = validate_layer(overrides, "command line options", require_version=False)
        sources.append(ConfigSource("options", None, None))
        _merge(settings, layer, "option", winners)
    return ResolvedConfig(settings, sources, winners, current)


def init_config(path: Path | None = None, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Create the user configuration from the example; never replace an edited file without --force."""
    target = (path or user_config_path()).expanduser().absolute()
    content = config_template()
    result: dict[str, Any] = {"path": str(target), "dry_run": dry_run, "backup_path": None}
    existing: str | None = None
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            existing = None
        if existing == content:
            return {**result, "status": "unchanged"}
        if not force:
            return {**result, "status": "preserved"}
    if dry_run:
        return {**result, "status": "would_replace" if target.exists() else "would_create"}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = target.with_name(f"{target.name}.{stamp}.bak")
        counter = 2
        while backup.exists():
            backup = target.with_name(f"{target.name}.{stamp}-{counter}.bak")
            counter += 1
        with backup.open("xb") as handle:
            handle.write(target.read_bytes())
        result["backup_path"] = str(backup)
    _write_atomic(target, content)
    return {**result, "status": "replaced" if result["backup_path"] else "created"}


def _write_atomic(target: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
