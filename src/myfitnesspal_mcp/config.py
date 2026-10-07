import os
from pathlib import Path

import platformdirs

APP_NAME = "myfitnesspal-mcp"

_TRUTHY = {"1", "true", "yes", "on"}


def truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def writes_allowed() -> bool:
    """Write tools stay off unless MFP_ALLOW_WRITES is set.

    MFP_READ_ONLY=1 forces them off even if MFP_ALLOW_WRITES is also set.
    """
    if truthy("MFP_READ_ONLY"):
        return False
    return truthy("MFP_ALLOW_WRITES")


def ensure_private_dir(path: Path) -> Path:
    """Create `path` and chmod 0700 when it is ours to lock down.

    A pre-existing override directory whose basename is not APP_NAME is left
    alone so `MFP_MCP_DATA_DIR=/tmp` cannot chmod a shared parent.
    """
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed or path.name == APP_NAME:
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def config_dir() -> Path:
    return ensure_private_dir(Path(platformdirs.user_config_dir(APP_NAME)))


def data_dir() -> Path:
    override = os.environ.get("MFP_MCP_DATA_DIR")
    if override:
        path = Path(override)
    else:
        path = Path(platformdirs.user_data_dir(APP_NAME))
    return ensure_private_dir(path)


def cookies_path() -> Path:
    return config_dir() / "cookies.json"


def database_path() -> Path:
    return data_dir() / "data.db"


def cookie_env() -> str | None:
    return os.environ.get("MFP_COOKIE")


def username_env() -> str | None:
    return os.environ.get("MFP_USERNAME")


def impersonate() -> str:
    return os.environ.get("MFP_IMPERSONATE", "chrome")


def sync_days() -> int:
    return int(os.environ.get("MFP_SYNC_DAYS", "30"))
