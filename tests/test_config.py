"""Settings: the .env file and the Azure OpenAI configuration."""

from __future__ import annotations

import os

import pytest

from t2pbi.config import load_azure_openai_config, load_env_file

_KEYS = (
    "T2PBI_AZURE_OPENAI_ENDPOINT",
    "T2PBI_AZURE_OPENAI_DEPLOYMENT",
    "T2PBI_AZURE_OPENAI_API_VERSION",
    "T2PBI_AZURE_OPENAI_API_KEY",
    "T2PBI_AZURE_OPENAI_TEMPERATURE",
    "T2PBI_TEST_PATH",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Start each test with these variables unset, remove whatever a test loads into them, and
    never read the developer's real .env."""

    for key in _KEYS:
        monkeypatch.setenv(key, "x")  # registers the original state for restoration...
        monkeypatch.delenv(key)  # ...then unsets it for the test
    monkeypatch.setenv("T2PBI_ENV_FILE", str(tmp_path / "no-such.env"))


def test_env_file_values_are_loaded(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "T2PBI_AZURE_OPENAI_ENDPOINT=https://example.openai.azure.com/\n"
        "T2PBI_TEST_PATH='C:\\Program Files\\Power BI\\bin\\PBIDesktop.exe'\n",
        encoding="utf-8",
    )

    assert load_env_file(env) == env
    assert os.environ["T2PBI_AZURE_OPENAI_ENDPOINT"] == "https://example.openai.azure.com/"
    # Single-quoted values are literal: Windows backslashes and spaces survive.
    assert os.environ["T2PBI_TEST_PATH"] == r"C:\Program Files\Power BI\bin\PBIDesktop.exe"


def test_real_environment_variables_win(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("T2PBI_AZURE_OPENAI_DEPLOYMENT=from-file\n", encoding="utf-8")
    monkeypatch.setenv("T2PBI_AZURE_OPENAI_DEPLOYMENT", "from-environment")

    load_env_file(env)

    assert os.environ["T2PBI_AZURE_OPENAI_DEPLOYMENT"] == "from-environment"


def test_missing_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "absent.env") is None


def test_empty_values_mean_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here or above in tmp, so only the variables below count
    monkeypatch.setenv("T2PBI_AZURE_OPENAI_ENDPOINT", "")
    monkeypatch.setenv("T2PBI_AZURE_OPENAI_DEPLOYMENT", "")
    monkeypatch.setenv("T2PBI_AZURE_OPENAI_API_KEY", "")

    config = load_azure_openai_config()

    assert config.endpoint is None
    assert config.deployment == "gpt-4o"
    assert config.api_version == "2024-08-01-preview"
    assert config.api_key is None
    assert config.temperature == 0.0
    assert not config.is_configured


@pytest.mark.parametrize(("raw", "expected"), [("0.2", 0.2), ("default", None), ("None", None)])
def test_temperature_setting(raw, expected, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("T2PBI_AZURE_OPENAI_TEMPERATURE", raw)
    assert load_azure_openai_config().temperature == expected


def test_bad_temperature_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("T2PBI_AZURE_OPENAI_TEMPERATURE", "cold")
    with pytest.raises(ValueError, match="number or 'default'"):
        load_azure_openai_config()
