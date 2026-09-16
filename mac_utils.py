"""macOS-specific paths and browser discovery for Linuxdoday."""

from pathlib import Path
import os


APP_NAME = "Linuxdoday"
PROJECT_DIR = Path(__file__).resolve().parent


def app_support_dir():
    """Return the writable per-user application directory used on macOS."""
    override = os.environ.get("LINUXDO_DATA_DIR")
    path = Path(override).expanduser() if override else Path.home() / "Library" / "Application Support" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def browser_data_dir():
    """Keep an existing project profile working, otherwise use Application Support."""
    override = os.environ.get("LINUXDO_BROWSER_DATA")
    if override:
        path = Path(override).expanduser()
    else:
        legacy = PROJECT_DIR / "browser_data"
        path = legacy if legacy.exists() else app_support_dir() / "browser_data"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def chrome_path():
    """Locate Google Chrome in the standard system and per-user macOS locations."""
    override = os.environ.get("LINUXDO_CHROME_PATH")
    candidates = [
        Path(override).expanduser() if override else None,
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    return None
