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


def test_oidc_secrets_and_existing_commands(tmp_path: Path) -> None:
    path = _secrets_file(
        tmp_path,
        "GOOGLE_CLIENT_ID=test-id\nGOOGLE_CLIENT_SECRET=test-secret\nKIROKU_SESSION_SECRET="
        + "s" * 48
        + "\nJEV_API_KEY=jev\nTELEGRAM_BOT_TOKEN=abc${HOME}\n",
    )
    env = {
        "KIROKU_SECRETS_FILE": str(path),
        "KIROKU_OWNER_EMAIL": "owner@example.test",
        "KIROKU_PUBLIC_URL": "https://kiroku.example/",
    }
    settings = Settings.load(env)
    assert settings.oidc is not None
    assert settings.oidc.client_id.reveal() == "test-id"
    assert settings.oidc.public_url == "https://kiroku.example"
    assert settings.secrets.require(SecretName.JEV).reveal() == "jev"
    assert settings.secrets.require(SecretName.TELEGRAM).reveal() == "abc${HOME}"
    assert "test-secret" not in repr(settings) and "s" * 48 not in repr(settings)
    assert Settings.load({"KIROKU_SECRETS_FILE": str(path)}).oidc is None
    settings = Settings.load(env | {"GOOGLE_CLIENT_SECRET": "override"})
    assert settings.oidc is not None and settings.oidc.client_secret.reveal() == "override"


@pytest.mark.parametrize(
    "missing",
    [
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "KIROKU_SESSION_SECRET",
        "KIROKU_OWNER_EMAIL",
        "KIROKU_PUBLIC_URL",
    ],
)
def test_partial_oidc_is_read_only(missing: str) -> None:
    env = {
        "KIROKU_SECRETS_FILE": "/absent",
        "GOOGLE_CLIENT_ID": "id",
        "GOOGLE_CLIENT_SECRET": "secret",
        "KIROKU_SESSION_SECRET": "s" * 48,
        "KIROKU_OWNER_EMAIL": "owner@example.test",
        "KIROKU_PUBLIC_URL": "https://kiroku.example",
    }
    env.pop(missing)
    assert Settings.load(env).oidc is None


@pytest.mark.parametrize(
    "url", ["http://kiroku.example", "https://kiroku.example/path", "https://kiroku.example?x=1"]
)
def test_oidc_requires_https_origin(url: str) -> None:
    env = {
        "KIROKU_SECRETS_FILE": "/absent",
        "GOOGLE_CLIENT_ID": "id",
        "GOOGLE_CLIENT_SECRET": "secret",
        "KIROKU_SESSION_SECRET": "s" * 48,
        "KIROKU_OWNER_EMAIL": "owner@example.test",
        "KIROKU_PUBLIC_URL": url,
    }
    with pytest.raises(ConfigError, match="HTTPS origin"):
        Settings.load(env)
    with pytest.raises(ConfigError, match="32 characters"):
        Settings.load(
            env | {"KIROKU_PUBLIC_URL": "https://kiroku.example", "KIROKU_SESSION_SECRET": "short"}
        )
