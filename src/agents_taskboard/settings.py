"""TASKBOARD_* settings of agents-taskboard."""

import ipaddress
from typing import Annotated
from urllib.parse import urlsplit

from agents_common.settings import BaseServiceSettings, CsvList, SettingsError
from agents_common.settings import load_settings as _load
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import NoDecode, SettingsConfigDict

__all__ = ["Settings", "SettingsError", "load_settings"]


class Settings(BaseServiceSettings):
    """Environment-driven settings shared by every process (web, service, notifier, migrate)."""

    model_config = SettingsConfigDict(env_prefix="TASKBOARD_", extra="ignore")

    allowed_cidrs: CsvList = ["127.0.0.1/32"]
    service_port: int = Field(8001, ge=1, le=65535)
    public_base_url: str
    relay_token: SecretStr = Field(repr=False)
    delegate_tokens: Annotated[list[SecretStr], NoDecode] = Field([], repr=False)
    delegate_port: int = Field(8003, ge=1, le=65535)
    notifier_poll_seconds: float = Field(5.0, gt=0)
    notifier_backoff_max_seconds: float = Field(60.0, gt=0)

    @field_validator("allowed_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(
                    f"TASKBOARD_{str(info.field_name).upper()}::not a CIDR: {item!r}"
                ) from None
        return value

    @field_validator("public_base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"TASKBOARD_PUBLIC_BASE_URL::not an absolute http(s) URL: {value!r}")
        return value.rstrip("/")

    @field_validator("relay_token")
    @classmethod
    def _token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("TASKBOARD_RELAY_TOKEN::required")
        return value

    @model_validator(mode="after")
    def _delegate(self) -> "Settings":
        relay = self.relay_token.get_secret_value()
        if any(t.get_secret_value() == relay for t in self.delegate_tokens):
            raise ValueError("TASKBOARD_DELEGATE_TOKENS::must differ from TASKBOARD_RELAY_TOKEN")
        if self.delegate_port in (self.http_port, self.service_port):
            raise ValueError(
                "TASKBOARD_DELEGATE_PORT::must differ from TASKBOARD_HTTP_PORT "
                "and TASKBOARD_SERVICE_PORT"
            )
        return self

    def api_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.allowed_cidrs]


def load_settings() -> Settings:
    """Load Settings from the environment or raise SettingsError naming every bad variable."""
    return _load(Settings)
