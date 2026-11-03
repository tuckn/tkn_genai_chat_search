"""Configuration layers, schema versions, validation and `config init`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import Runner

from tkn_genai_chat_search.config import (
    SCHEMA_VERSION,
    ConfigError,
    config_template,
    init_config,
    load_config,
    user_config_path,
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_five_layers_apply_in_order(isolated: Path) -> None:
    write(user_config_path(), 'schema_version: "1.0.0"\ndefaults:\n  limit: 1\n  order: asc\n  sort_by: created\n')
    write(Path.cwd() / ".tkn" / "config.yaml", 'schema_version: "1.0.0"\ndefaults:\n  limit: 2\n  order: desc\n')
    explicit = write(isolated / "explicit.yaml", 'schema_version: "1.0.0"\ndefaults:\n  limit: 3\n')
    config = load_config(explicit, overrides={"defaults": {"limit": 4}})
    assert config.defaults["limit"] == 4
    assert config.defaults["order"] == "desc"
    assert config.defaults["sort_by"] == "created"
    assert config.defaults["max_message_chars"] == 1200
    assert config.value_sources["defaults.limit"] == "option"
    assert config.value_sources["defaults.order"] == "config:project"
    assert config.value_sources["defaults.sort_by"] == "config:user"
    assert config.value_sources["defaults.max_message_chars"] == "built-in"
    assert [s.name for s in config.sources] == ["built-in", "user", "project", "explicit", "options"]


def test_schema_version_is_layer_metadata_not_a_setting(isolated: Path) -> None:
    write(user_config_path(), 'schema_version: "1.0.3"\n')
    config = load_config()
    assert "schema_version" not in config.settings
    assert config.sources[1].schema_version == "1.0.3"
    with pytest.raises(ConfigError):
        load_config(overrides={"schema_version": "1.0.0"})


@pytest.mark.parametrize("version", ["1.0.0", "1.0.9"])
def test_supported_versions(isolated: Path, version: str) -> None:
    write(user_config_path(), f'schema_version: "{version}"\n')
    assert load_config().sources[1].schema_version == version


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("defaults:\n  limit: 1\n", "schema_version is required"),
        ("schema_version: 1.0\n", "quoted"),
        ('schema_version: "1.0"\n', "quoted"),
        ('schema_version: "1.1.0"\n', "not supported"),
        ('schema_version: "2.0.0"\n', "not supported"),
        ('schema_version: "1.0.0"\nlimit: 1\n', "unknown keys: limit"),
        ('schema_version: "1.0.0"\ndefaults:\n  page_size: 1\n', "unknown defaults keys"),
        ('schema_version: "1.0.0"\nroots:\n  chatgpt: []\n', "unknown roots keys"),
        ('schema_version: "1.0.0"\nroots:\n  codex: C:/single\n', "roots.codex"),
        ('schema_version: "1.0.0"\ndefaults:\n  limit: "10"\n', "defaults.limit"),
        ('schema_version: "1.0.0"\ndefaults:\n  limit: true\n', "defaults.limit"),
        ('schema_version: "1.0.0"\ndefaults:\n  order: newest\n', "defaults.order"),
        ('schema_version: "1.0.0"\ndefaults:\n  utc_offset: 9\n', "utc_offset"),
        ('schema_version: "1.0.0"\ndefaults:\n  limit: 1\n  limit: 2\n', "duplicate key"),
        ("", "empty"),
        ("- a\n", "mapping"),
    ],
)
def test_invalid_layers_are_errors(isolated: Path, text: str, message: str) -> None:
    path = write(user_config_path(), text)
    with pytest.raises(ConfigError) as raised:
        load_config()
    assert message in str(raised.value)
    assert str(path) in str(raised.value)


def test_explicit_file_must_exist(isolated: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(isolated / "absent.yaml")


def test_config_roots_resolve_relative_to_current_folder(isolated: Path) -> None:
    write(user_config_path(), "schema_version: \"1.0.0\"\nroots:\n  codex: ['logs/.codex', '~/.codex-copy']\n")
    config = load_config()
    roots, explicit = config.roots("codex", [])
    assert explicit
    assert [root.path for root in roots] == [Path.cwd() / "logs/.codex", Path.home() / ".codex-copy"]
    assert {root.origin for root in roots} == {"config:user"}


def test_example_is_valid_and_starts_with_quoted_schema_version() -> None:
    text = config_template()
    first_key = next(line for line in text.splitlines() if line and not line.startswith("#"))
    assert first_key == f'schema_version: "{SCHEMA_VERSION}"'
    from tkn_genai_chat_search.config import validate_layer

    validate_layer(yaml.safe_load(text), "example")


def test_init_creates_then_reports_unchanged_and_protects_edits(isolated: Path) -> None:
    created = init_config()
    assert created["status"] == "created"
    path = Path(created["path"])
    assert path == user_config_path()
    assert path.read_text(encoding="utf-8") == config_template()
    assert init_config()["status"] == "unchanged"
    path.write_text(config_template() + "# edited\n", encoding="utf-8")
    assert init_config()["status"] == "preserved"
    assert init_config(force=True, dry_run=True)["status"] == "would_replace"
    assert path.read_text(encoding="utf-8").endswith("# edited\n")
    replaced = init_config(force=True)
    assert replaced["status"] == "replaced"
    assert Path(replaced["backup_path"]).read_text(encoding="utf-8").endswith("# edited\n")
    assert path.read_text(encoding="utf-8") == config_template()


def test_init_dry_run_writes_nothing(isolated: Path) -> None:
    result = init_config(dry_run=True)
    assert result["status"] == "would_create"
    assert not user_config_path().exists()


def test_config_commands_through_the_cli(run: Runner, isolated: Path) -> None:
    result = run("config", "init")
    assert result.code == 0
    assert "[SUCCESS] Configuration created" in result.err
    assert f"path={user_config_path()}" in result.out.splitlines()
    user_config_path().write_text('schema_version: "1.0.0"\ndefaults:\n  limit: 5\n', encoding="utf-8")
    kept = run("config", "init")
    assert kept.code == 0 and "status=preserved" in kept.out and "[WARNING]" in kept.err
    listed = run("config", "list")
    lines = listed.out.splitlines()
    assert "settings.defaults.limit=5" in lines
    assert "value_sources.defaults.limit=config:user" in lines
    assert "settings.roots.codex=null" in lines
    assert any(line.startswith("resolved_roots.codex[0].path=") for line in lines)
    assert "[INFO] Showing resolved configuration" in listed.err
    as_json = run("config", "list", "--json").json
    assert as_json["effective_schema_version"] == SCHEMA_VERSION
    assert [s["name"] for s in as_json["config_sources"]] == ["built-in", "user"]


def test_invalid_configuration_fails_with_exit_1(run: Runner) -> None:
    write(Path.cwd() / ".tkn" / "config.yaml", 'schema_version: "1.0.0"\nprofiles: {}\n')
    result = run("list", "--json")
    assert result.code == 1
    assert "unknown keys: profiles" in result.json["error"]
    assert result.json["error_path"].endswith("config.yaml")


def test_configured_limit_applies_and_option_overrides_it(run: Runner, isolated: Path) -> None:
    from conftest import CodexHome, thread_id

    codex = CodexHome(isolated / "codex")
    for number in range(3):
        codex.rollout(thread_id(number))
    write(
        user_config_path(),
        f"schema_version: \"1.0.0\"\nroots:\n  codex: ['{codex.root.as_posix()}']\ndefaults:\n  limit: 1\n",
    )
    assert len(run("list", "--source", "codex", "--json").json["sessions"]) == 1
    assert len(run("list", "--source", "codex", "--json", "--limit", "0").json["sessions"]) == 3
    missing = write(isolated / "bad.yaml", "schema_version: \"1.0.0\"\nroots:\n  codex: ['C:/no/such/folder']\n")
    result = run("list", "--source", "codex", "--json", "--config", str(missing))
    assert result.code == 1
    assert "folder not found" in result.json["error"]
