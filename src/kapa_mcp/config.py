# SPDX-License-Identifier: GPL-3.0-only
"""Explicit local configuration, without credential discovery or file reads."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat


REPOSITORIES = ("kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge")


class ConfigurationError(ValueError):
    """A configuration error safe to report on stderr."""


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
