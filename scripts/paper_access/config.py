import json
import os
from pathlib import Path

METHODS = {"cstcloud", "carsi", "publisher", "webvpn", "ezproxy", "api"}


class PaperError(Exception):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.result = {"status": status, "message": message, **details}


def load_config(filename=None):
    filename = filename or os.environ.get("PAPER_ACCESS_CONFIG")
    base = Path(filename).expanduser().resolve().parent if filename else Path.cwd()
    cfg = {"timeout": 30, "browser_timeout": 90, "pdf_timeout": 60, "max_bytes": 80 * 1024 * 1024,
           "delay_seconds": 15, "profiles": {}, "publishers": {},
           "data_dir": os.environ.get("PAPER_ACCESS_DATA_DIR", str(Path.home() / ".paper-access"))}
    if filename:
        cfg.update(json.loads(Path(filename).expanduser().read_text(encoding="utf-8-sig")))
    root = Path(cfg["data_dir"]).expanduser()
    cfg["data_dir"] = str((base / root).resolve() if not root.is_absolute() else root.resolve())
    for name, profile in cfg["profiles"].items():
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in name):
            raise PaperError("configuration_error", "Profile names require letters, numbers, hyphens or underscores")
        if profile.get("method") not in METHODS:
            raise PaperError("configuration_error", f"Unknown authentication method for profile {name}")
    for key in ("timeout", "browser_timeout", "pdf_timeout", "max_bytes"):
        if not isinstance(cfg[key], (int, float)) or cfg[key] <= 0:
            raise PaperError("configuration_error", f"{key} must be positive")
    if not isinstance(cfg["delay_seconds"], (int, float)) or cfg["delay_seconds"] < 0:
        raise PaperError("configuration_error", "delay_seconds must be nonnegative")
    return cfg


def profile_for(cfg, name):
    if name not in cfg["profiles"]:
        raise PaperError("configuration_error", f"Unknown profile: {name}")
    return cfg["profiles"][name]


def host_matches(host, domains):
    host = (host or "").lower().rstrip(".")
    return any(host == d.lower().rstrip(".") or host.endswith("." + d.lower().rstrip(".")) for d in domains if d)


def publisher_config(cfg, url):
    from urllib.parse import urlsplit
    return next((v for v in cfg["publishers"].values()
                 if host_matches(urlsplit(url).hostname, v.get("domains", []))), {})
