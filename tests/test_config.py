from pathlib import Path

import pytest

from eikocode import config
from eikocode.config import Config, is_known_model, load_config
from eikocode.errors import ErrorKind, EikoCodeError

ENV_NAMES = (
    "EIKOCODE_MODEL",
    "EIKOCODE_ANTHROPIC_API_KEY",
    "EIKOCODE_OPENAI_API_KEY",
    "EIKOCODE_OPENAI_BASE_URL",
    "EIKOCODE_AUTO_APPROVE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield


def test_priority_env_beats_project_beats_user(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    user.write_text('model = "from-user"\n', encoding="utf-8")
    monkeypatch.setattr(config, "USER_CONFIG_PATH", user)

    project = tmp_path / "project"
    project.mkdir()
    (project / ".eikocode.toml").write_text('model = "from-project"\n', encoding="utf-8")

    assert load_config(cwd=project).model == "from-project"

    monkeypatch.setenv("EIKOCODE_MODEL", "from-env")
    assert load_config(cwd=project).model == "from-env"


def test_falls_back_to_user_config_when_project_absent(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    user.write_text('model = "from-user"\n', encoding="utf-8")
    monkeypatch.setattr(config, "USER_CONFIG_PATH", user)

    assert load_config(cwd=tmp_path).model == "from-user"


def test_defaults_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "missing.toml")
    cfg = load_config(cwd=tmp_path)

    assert cfg.temperature == 0.7
    assert cfg.max_tokens == 4096
    assert cfg.context_limit == 200000


def test_credentials_come_from_environment_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "missing.toml")

    cfg = load_config(cwd=tmp_path)
    assert not cfg.has_any_credential

    monkeypatch.setenv("EIKOCODE_ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = load_config(cwd=tmp_path)
    assert cfg.anthropic_api_key == "sk-ant-test"
    assert cfg.has_any_credential


def test_config_file_cannot_supply_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "missing.toml")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".eikocode.toml").write_text(
        'anthropic_api_key = "leaked"\n', encoding="utf-8"
    )

    cfg = load_config(cwd=project)
    assert cfg.anthropic_api_key is None


def test_broken_toml_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "missing.toml")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".eikocode.toml").write_text("this is not toml = = =\n", encoding="utf-8")

    with pytest.raises(EikoCodeError) as excinfo:
        load_config(cwd=project)
    assert excinfo.value.kind is ErrorKind.CONFIG


@pytest.mark.parametrize(
    "name,expected",
    [
        ("claude-sonnet-5", True),
        ("gpt-4o", True),
        ("deepseek-chat", True),
        ("nope", False),
        ("", False),
        ("some model", False),
    ],
)
def test_is_known_model(name, expected):
    assert is_known_model(name) is expected


def test_is_known_model_respects_extra_prefixes():
    assert is_known_model("my-llm-v2") is False
    assert is_known_model("my-llm-v2", extra=("my-llm",)) is True


def test_config_module_has_no_write_path():
    """凭据永不落盘这条规矩用源码扫描来兜底。"""
    source = Path(config.__file__).read_text(encoding="utf-8")
    assert "write(" not in source
    assert "json.dump" not in source
    assert "toml.dump" not in source


def test_sample_config_never_carries_a_key():
    example = Path(__file__).resolve().parent.parent / ".eikocode.toml.example"
    text = example.read_text(encoding="utf-8")
    assert "API_KEY" not in text


# --------------------------------------------------------------------------- #
# 自动批准开关（auto_approve）
# --------------------------------------------------------------------------- #
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "missing.toml")
    return tmp_path


def test_auto_approve_defaults_off(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    assert load_config(cwd=tmp_path).auto_approve is False


def test_auto_approve_truthy_env_values(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    for value in ("1", "on", "true", "yes", "TRUE", "On"):
        monkeypatch.setenv("EIKOCODE_AUTO_APPROVE", value)
        assert load_config(cwd=tmp_path).auto_approve is True, value


def test_auto_approve_falsy_env_values(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    for value in ("0", "off", "false", "no", "none", "OFF"):
        monkeypatch.setenv("EIKOCODE_AUTO_APPROVE", value)
        assert load_config(cwd=tmp_path).auto_approve is False, value


def test_auto_approve_reads_project_config(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".eikocode.toml").write_text("auto_approve = true\n", encoding="utf-8")

    assert load_config(cwd=project).auto_approve is True


def test_auto_approve_env_beats_project_config(tmp_path, monkeypatch):
    """环境变量优先：配置里开着也能被 EIKOCODE_AUTO_APPROVE=off 关掉。"""
    _isolated(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".eikocode.toml").write_text("auto_approve = true\n", encoding="utf-8")
    assert load_config(cwd=project).auto_approve is True

    monkeypatch.setenv("EIKOCODE_AUTO_APPROVE", "off")
    assert load_config(cwd=project).auto_approve is False


def test_empty_env_falls_back_to_config(tmp_path, monkeypatch):
    """空串按「未设置」处理，落到配置值，而不是当成假把配置关掉。"""
    _isolated(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".eikocode.toml").write_text("auto_approve = true\n", encoding="utf-8")
    monkeypatch.setenv("EIKOCODE_AUTO_APPROVE", "")

    assert load_config(cwd=project).auto_approve is True
