"""Optional manual bootstrap in a normally launched, dedicated system browser."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

from .config import PaperError, profile_for
from .storage import ProfileLock, atomic_write


def connection_file(cfg, name):
    profile_for(cfg, name)
    return Path(cfg["data_dir"]) / "profiles" / name / "browser-connection.json"


def endpoint_ready(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{int(port)}/json/version", timeout=1) as response:
            return "webSocketDebuggerUrl" in json.load(response)
    except Exception:
        return False


def connection(cfg, name):
    path = connection_file(cfg, name)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if endpoint_ready(state["port"]):
            return "http://127.0.0.1:" + str(int(state["port"]))
    except (ValueError, OSError, KeyError):
        pass
    raise PaperError("auth_required", "Dedicated system browser is not running; run login-open first", profile=name)


def browser_binary(cfg):
    if cfg.get("browser_executable"):
        candidate = Path(cfg["browser_executable"]).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise PaperError("configuration_error", "Configured browser_executable does not exist")
    channel = cfg.get("browser_channel", "msedge" if os.name == "nt" else "chrome")
    candidates = []
    if os.name == "nt":
        suffix = "Microsoft/Edge/Application/msedge.exe" if channel == "msedge" else "Google/Chrome/Application/chrome.exe"
        candidates = [Path(os.environ.get(key, "C:/Program Files")) / suffix for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    else:
        for name in (("microsoft-edge", "microsoft-edge-stable") if channel == "msedge" else ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        if sys_platform() == "darwin":
            candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise PaperError("dependency_error", "Install a system Chrome/Edge browser or set browser_executable")


def sys_platform():
    import sys
    return sys.platform


def open_login(cfg, name, url):
    profile = profile_for(cfg, name)
    if profile["method"] == "api":
        raise PaperError("configuration_error", "API profiles do not use browser login")
    if urlsplit(url).scheme not in {"http", "https"}:
        raise PaperError("configuration_error", "Login URL must be HTTP(S)")
    state_file = connection_file(cfg, name)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        state_file.parent.chmod(0o700)
    with ProfileLock(state_file.parent / "process.lock"):
        try:
            connection(cfg, name)
            return {"status": "awaiting_login", "profile": name, "reused": True,
                    "message": "Use the existing dedicated browser; fetch connects only after you finish login"}
        except PaperError:
            pass
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        args = [browser_binary(cfg), "--user-data-dir=" + str(state_file.parent / "system-browser"),
                "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(port),
                "--no-first-run", "--no-default-browser-check", url]
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            if endpoint_ready(port):
                atomic_write(state_file, json.dumps({"port": port, "pid": process.pid, "created": time.time()}).encode())
                return {"status": "awaiting_login", "profile": name, "reused": False,
                        "message": "Complete official website verification and login in the dedicated browser, then run fetch with external_browser=true"}
            time.sleep(0.3)
    raise PaperError("dependency_error", "Dedicated browser did not expose its local connection endpoint")
