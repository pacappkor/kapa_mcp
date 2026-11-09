# SPDX-License-Identifier: GPL-3.0-only
"""Explicit local configuration, without credential discovery or file reads."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat


REPOSITORIES = ("kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge")


class ConfigurationError(ValueError):
    """A configuration error safe to report on stderr."""


@dataclass(frozen=True)
class HTTPSettings:
    """Private transport options; the bearer is never included in repr/errors."""

    token: str = field(repr=False)
    port: int = 8765

    def __post_init__(self) -> None:
        if (not isinstance(self.token, str) or len(self.token) < 32
                or not self.token.isascii() or any(c.isspace() for c in self.token)
                or any(ord(c) < 33 or ord(c) > 126 for c in self.token)):
            raise ConfigurationError("KAPA_HTTP_TOKEN must contain at least 32 printable ASCII characters without whitespace")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ConfigurationError("KAPA_HTTP_PORT must be an integer from 1024 through 65535")

    @classmethod
    def from_env(cls) -> "HTTPSettings":
        try:
            port = int(os.environ.get("KAPA_HTTP_PORT", "8765"))
        except ValueError:
            raise ConfigurationError("KAPA_HTTP_PORT must be an integer from 1024 through 65535") from None
        return cls(token=os.environ.get("KAPA_HTTP_TOKEN", ""), port=port)


def transport_from_env() -> str:
    transport = os.environ.get("KAPA_TRANSPORT", "stdio")
    if transport not in ("stdio", "streamable-http"):
        raise ConfigurationError("KAPA_TRANSPORT must be stdio or streamable-http")
    return transport


@dataclass(frozen=True)
class Settings:
    workspace: Path
    jobs_dir: Path

    def __post_init__(self) -> None:
        for field, variable in (("workspace", "KAPA_WORKSPACE"), ("jobs_dir", "KAPA_JOBS_DIR")):
            path = Path(getattr(self, field))
            if not path.is_absolute():
                raise ConfigurationError(f"{variable} must be an absolute directory path")
            try:
                # Inspect before resolve so links cannot disappear before readers
                # and renderer perform their own path checks.
                for component in (path, *path.parents):
                    try:
                        info = component.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ConfigurationError(f"{variable} must not contain links or reparse points")
                path = path.resolve()
            except ConfigurationError:
                raise
            except (OSError, RuntimeError, ValueError):
                raise ConfigurationError(f"{variable} is not a valid directory path") from None
            object.__setattr__(self, field, path)
        if not self.workspace.is_dir():
            raise ConfigurationError("KAPA_WORKSPACE must be an existing directory")
        if self.jobs_dir.exists() and not self.jobs_dir.is_dir():
            raise ConfigurationError("KAPA_JOBS_DIR must be a directory")
        # A renderer must never write into an upstream checkout, even through a link.
        for alias in (*REPOSITORIES, "kapa_mcp"):
            root = (self.workspace / alias).resolve()
            if self.jobs_dir.is_relative_to(root) or root.is_relative_to(self.jobs_dir):
                raise ConfigurationError("KAPA_JOBS_DIR must be separate from source repositories")

    @classmethod
    def from_env(cls) -> "Settings":
        values = {}
        for name in ("KAPA_WORKSPACE", "KAPA_JOBS_DIR"):
            value = os.environ.get(name)
            if value is None or not value.strip():
                raise ConfigurationError(f"{name} is required; set an explicit absolute directory path")
            values[name] = Path(value)
        return cls(workspace=values["KAPA_WORKSPACE"], jobs_dir=values["KAPA_JOBS_DIR"])
