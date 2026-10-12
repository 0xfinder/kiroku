from decimal import Decimal
from pathlib import Path

import pytest

from kiroku.config import ConfigError, Secret, SecretName, Settings


def _secrets_file(tmp_path: Path, body: str, mode: int = 0o600) -> Path:
    path = tmp_path / "secrets.env"
    path.write_text(body)
    path.chmod(mode)
    return path


def test_missing_secret_names_the_variable_and_file() -> None:
    settings = Settings.load({"KIROKU_SECRETS_FILE": "/nonexistent/secrets.env"})
    with pytest.raises(ConfigError, match=r"GROQ_API_KEY is not set.*/nonexistent/secrets.env"):
        settings.secrets.require(SecretName.GROQ)


def test_secret_is_read_from_file(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, "JEV_API_KEY=from-file\n")
    settings = Settings.load({"KIROKU_SECRETS_FILE": str(path)})
    assert settings.secrets.require(SecretName.JEV).reveal() == "from-file"


def test_environment_overrides_file(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, "GROQ_API_KEY=from-file\n")
    settings = Settings.load({"KIROKU_SECRETS_FILE": str(path), "GROQ_API_KEY": "from-env"})
    assert settings.secrets.require(SecretName.GROQ).reveal() == "from-env"


def test_secrets_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, "GROQ_API_KEY=x\n", mode=0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        Settings.load({"KIROKU_SECRETS_FILE": str(path)})


def test_secret_values_never_appear_in_repr(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, "GROQ_API_KEY=gsk_live_value\n")
    settings = Settings.load({"KIROKU_SECRETS_FILE": str(path)})
    assert "gsk_live_value" not in repr(settings)
    assert "gsk_live_value" not in str(Secret("gsk_live_value"))


def test_empty_environment_variable_does_not_hide_file_value(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, "GROQ_API_KEY=from-file\n")
    settings = Settings.load({"KIROKU_SECRETS_FILE": str(path), "GROQ_API_KEY": ""})
    assert settings.secrets.require(SecretName.GROQ).reveal() == "from-file"


def test_secret_values_are_not_interpolated(tmp_path: Path) -> None:
    path = _secrets_file(tmp_path, "TELEGRAM_BOT_TOKEN=abc${HOME}def\n")
    settings = Settings.load({"KIROKU_SECRETS_FILE": str(path)})
    assert settings.secrets.require(SecretName.TELEGRAM).reveal() == "abc${HOME}def"


def test_budget_caps_default_to_d8_and_can_be_lowered() -> None:
    caps = Settings.load({"KIROKU_SECRETS_FILE": "/x"}).caps
    assert (caps.month_usd, caps.session_usd) == (Decimal(20), Decimal("1.50"))
    env = {"KIROKU_SECRETS_FILE": "/x", "KIROKU_SESSION_CAP_USD": "0.25"}
    assert Settings.load(env).caps.session_usd == Decimal("0.25")


@pytest.mark.parametrize("value", ["1,50", "-1", "NaN", "inf", "$2"])
def test_an_unreadable_cap_is_an_error_not_no_cap(value: str) -> None:
    env = {"KIROKU_SECRETS_FILE": "/x", "KIROKU_MONTH_CAP_USD": value}
    with pytest.raises(ConfigError, match="KIROKU_MONTH_CAP_USD"):
        Settings.load(env)
