"""Locate read-only resources and writable, version-independent user data.

Path discovery has no filesystem side effects. Tests can inject ``AppPaths``;
normal startup alone is responsible for running the copy-only legacy import.
"""
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path, user_config_path, user_data_path, user_log_path


@dataclass(frozen=True)
class AppPaths:
    resource_root: Path
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path

    @classmethod
    def discover(cls, resource_root: Path) -> "AppPaths":
        name = "STARRYPAD"
        return cls(
            Path(resource_root).resolve(),
            user_config_path(name, appauthor=False),
            user_data_path(name, appauthor=False),
            user_cache_path(name, appauthor=False),
            user_log_path(name, appauthor=False),
        )

    @classmethod
    def under(cls, root: Path, resource_root: Path) -> "AppPaths":
        """An explicit, isolated layout for tests and development."""
        root = Path(root).resolve()
        return cls(Path(resource_root).resolve(), root / "config", root / "data",
                   root / "cache", root / "logs")

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "drum_pad_settings.json"

    @property
    def projects(self) -> Path:
        return self.data_dir / "projects"

    @property
    def samples(self) -> Path:
        return self.data_dir / "user-samples"

    @property
    def exports(self) -> Path:
        return self.data_dir / "exports"
