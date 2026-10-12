"""Settings and secrets. The only module that reads the environment or secrets.env.

Loading never fails for a missing API key. A secret is demanded only where the component
that needs it is built (`settings.secrets.require(...)` in kiroku.cli), so commands like
`kiroku db migrate` run without any keys. The process environment overrides secrets.env.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

from kiroku.budget import Caps
from kiroku.errors import KirokuError

DEFAULT_SECRETS_FILE = Path("~/.config/kiroku/secrets.env").expanduser()
DEFAULT_DATABASE_URL = "postgresql://kiroku:kiroku@127.0.0.1:5432/kiroku"
DEFAULT_DATA_DIR = Path("~/.local/share/kiroku").expanduser()
DEFAULT_CAPS = Caps()


class SecretName(StrEnum):
    """Every secret kiroku knows. Add a member here; nothing else lists them."""

    GROQ = "GROQ_API_KEY"
    JEV = "JEV_API_KEY"
    ANTHROPIC = "ANTHROPIC_API_KEY"
    TELEGRAM = "TELEGRAM_BOT_TOKEN"
    MUSE = "MUSE_API_KEY"


class ConfigError(KirokuError):
    pass


@dataclass(frozen=True, slots=True)
class Secret:
    """An API key. `repr` and `str` never show the value; `reveal()` is greppable."""

    _value: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class Secrets:
    values: Mapping[str, str]
    source_file: Path

    def __repr__(self) -> str:
        return f"Secrets(names={sorted(self.values)}, source_file={self.source_file})"

    def require(self, name: SecretName) -> Secret:
        value = self.values.get(name.value, "").strip()
        if not value:
            raise ConfigError(
                f"{name.value} is not set. Add `{name.value}=...` to {self.source_file} "
                "(mode 600) or export it in the environment."
            )
        return Secret(value)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    data_dir: Path  # audio archive: <data_dir>/audio/<channel>/<session>/<leg>.opus
    secrets: Secrets
    caps: Caps  # D8 Budget caps, in USD
    lolesports_api_key: str | None = None  # public website key override; not a Secret

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read the environment and secrets.env once. Tests pass `env` instead of patching."""
        env = os.environ if env is None else env
        path = Path(env.get("KIROKU_SECRETS_FILE", DEFAULT_SECRETS_FILE)).expanduser()
        values = read_secrets_file(path) | {
            n.value: env[n.value] for n in SecretName if env.get(n.value)
        }
        return cls(
            database_url=env.get("KIROKU_DATABASE_URL", DEFAULT_DATABASE_URL),
            data_dir=Path(env.get("KIROKU_DATA_DIR", DEFAULT_DATA_DIR)).expanduser(),
            secrets=Secrets(values, path),
            caps=Caps(
                session_usd=_usd(env, "KIROKU_SESSION_CAP_USD", DEFAULT_CAPS.session_usd),
                month_usd=_usd(env, "KIROKU_MONTH_CAP_USD", DEFAULT_CAPS.month_usd),
            ),
            lolesports_api_key=env.get("KIROKU_LOLESPORTS_API_KEY"),
        )


def _usd(env: Mapping[str, str], name: str, default: Decimal) -> Decimal:
    """A cap in USD. A typo must not silently lift a cap, so anything unreadable is an error."""
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{name}={raw!r} is not an amount in USD, e.g. 1.50")
    return value


def read_secrets_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines without touching os.environ. An absent file is empty."""
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise ConfigError(f"{path} is readable by other users. Run `chmod 600 {path}`.")
    return {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}
