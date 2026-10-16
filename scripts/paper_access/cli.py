import argparse
import json
import logging
from pathlib import Path
import sys

from .browser import Browser, auth_status
from .config import PaperError, load_config
from .core import batch, doctor, fetch
from .external import open_login


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise PaperError("configuration_error", "Invalid command arguments; run --help for usage")


def parser():
    root = JsonParser(description="Portable academic paper retrieval; outputs JSON")
    root.add_argument("--config", help="JSON configuration path (or PAPER_ACCESS_CONFIG)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    for command in ("fetch", "batch"):
        sub = commands.add_parser(command)
        if command == "fetch":
            sub.add_argument("--doi")
            sub.add_argument("--title")
            sub.add_argument("--url")
        else:
            sub.add_argument("--input", required=True)
        sub.add_argument("--output", help="Destination directory; default DATA_DIR/papers")
        sub.add_argument("--profile", help="Configured authentication profile")
        sub.add_argument("--oa-only", action="store_true")
        sub.add_argument("--headed", action="store_true")
    auth = commands.add_parser("auth")
    auth.add_argument("--profile", required=True)
    auth.add_argument("--url", required=True, help="Publisher article or institution login URL")
    auth.add_argument("--headed", action="store_true")
    status = commands.add_parser("auth-status")
    status.add_argument("--profile", required=True)
    manual = commands.add_parser("login-open", help="Open a dedicated system browser for manual first login")
    manual.add_argument("--profile", required=True)
    manual.add_argument("--url", required=True)
    return root


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(stream=sys.stderr, level=logging.ERROR)
    # PDF parser diagnostics go to stderr and never corrupt the JSON protocol.
    try:
        args = parser().parse_args(argv)
        cfg = load_config(args.config)
        if args.command == "doctor":
            result = doctor(cfg)
        elif args.command == "auth-status":
            result = auth_status(cfg, args.profile)
        elif args.command == "auth":
            with Browser(cfg, args.profile, args.headed) as browser:
                result = browser.authenticate(args.url)
                result.pop("target", None)  # May be a signed gateway URL.
        elif args.command == "login-open":
            result = open_login(cfg, args.profile, args.url)
        else:
            output = args.output or str(Path(cfg["data_dir"]) / "papers")
            options = {"profile": args.profile, "oa_only": args.oa_only, "headed": args.headed}
            if args.command == "fetch":
                if not any((args.doi, args.title, args.url)):
                    raise PaperError("configuration_error", "fetch requires --doi, --title, or --url")
                result = fetch(cfg, output, doi=args.doi, title=args.title, url=args.url, **options)
            else:
                result = batch(cfg, args.input, output, **options)
        if result["status"] in {"configuration_error", "dependency_error"}:
            code = 3
        else:
            code = 0 if result["status"] in {"ready", "stored_state", "authenticated", "session_observed", "awaiting_login", "candidates", "downloaded", "cached", "completed"} else 2
    except PaperError as exc:
        result = exc.result
        code = 3 if result["status"] in {"configuration_error", "dependency_error"} else 2
    except (OSError, ValueError, TypeError, KeyError):
        result, code = {"status": "configuration_error", "message": "Cannot read configuration/input or configuration has invalid fields"}, 3
    except KeyboardInterrupt:
        result, code = {"status": "cancelled", "message": "Command interrupted; completed files remain cached"}, 2
    except Exception:
        # Browser exception messages can contain passwords, selectors, or signed URLs.
        result, code = {"status": "execution_error", "message": "Operation failed; check the configured selectors and dependencies"}, 2
    print(json.dumps(result, ensure_ascii=False))
    return code
