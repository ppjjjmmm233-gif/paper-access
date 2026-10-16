import email.utils
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .config import PaperError


def retry_seconds(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int(email.utils.parsedate_to_datetime(value).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            return 900


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, cfg, headers=None, allow_redirects=True):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise PaperError("configuration_error", "Expected an HTTP(S) URL without embedded credentials")
    request = urllib.request.Request(url, headers={"User-Agent": "paper-access/0.1 (personal research)", **(headers or {})})
    try:
        opener = urllib.request.build_opener() if allow_redirects else urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=cfg["timeout"]) as response:
            data = response.read(int(cfg["max_bytes"]) + 1)
            if len(data) > cfg["max_bytes"]:
                raise PaperError("invalid_pdf", "Response exceeds configured size limit")
            return data, response.url, dict(response.headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise PaperError("rate_limited", "Remote service requested cooldown", host=parsed.hostname,
                             retry_after=retry_seconds(exc.headers.get("Retry-After"))) from None
        if exc.code == 401:
            raise PaperError("auth_required", "Remote service requires authentication") from None
        if exc.code == 403:
            raise PaperError("access_denied", "HTTP 403: entitlement or verification is unresolved") from None
        raise PaperError("network_error", f"Remote service returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PaperError("network_error", "Connection failed or timed out") from None


def get_json(url, cfg):
    data, _, _ = http_get(url, cfg, {"Accept": "application/json"})
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise PaperError("network_error", "Metadata service did not return JSON") from None
