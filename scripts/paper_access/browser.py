"""Bounded browser authentication. No secrets enter return values or logs."""
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from .config import PaperError, host_matches, profile_for, publisher_config
from .metadata import parse_article
from .net import retry_seconds
from .storage import ProfileLock, atomic_write
from .routes import main_pdf_urls, matches_article

LOGIN = re.compile(r"access through (?:your|an) institution|institutional (?:login|sign in)|sign in (?:via|through) (?:your )?institution|机构登录|通过.*机构访问|机构账号登录", re.I)
CHALLENGE = re.compile(r"verify (?:you are|you're) human|security verification|验证码|人机验证|enter (?:the )?(?:verification|one.time) code|two.factor authentication", re.I)
NO_ACCESS = re.compile(r"your institution (?:does not|doesn't) (?:have|provide) access|not included in your (?:institution.*)?subscription|您所在机构未订购|没有全文访问权限", re.I)
DEFAULT_USER = 'input[autocomplete="username"], input[name="username"], input[name="userName"], input[name="email"], input[name="loginName"], input[type="email"]'
DEFAULT_PASSWORD = 'input[type="password"]'
DEFAULT_SUBMIT = 'button[type="submit"], input[type="submit"], button:has-text("登录"), button:has-text("Sign in"), button:has-text("Log in")'


def public_url(url):
    """Diagnostic URLs exclude queries, fragments, and embedded credentials."""
    p = urlsplit(url)
    host = p.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if p.port:
        host += ":" + str(p.port)
    return f"{p.scheme}://{host}{p.path}"


def render_url(template, article_url, doi=""):
    p = urlsplit(article_url)
    return template.format(url=quote(article_url, safe=""), raw_url=article_url,
                           host=p.hostname or "", host_dashed=(p.hostname or "").replace(".", "-"),
                           path=p.path, query=p.query, doi=quote(doi, safe="/"))


def visible(scope, selector):
    if not selector:
        return None
    try:
        items = scope.locator(selector)
        for i in range(min(items.count(), 12)):
            item = items.nth(i)
            if item.is_visible():
                return item
    except Exception:
        return None
    return None


class Browser:
    def __init__(self, cfg, name, headed=False):
        self.cfg, self.name, self.profile = cfg, name, profile_for(cfg, name)
        self.root = Path(cfg["data_dir"]) / "profiles" / name
        self.headed, self.captured, self.blocked = headed, [], None
        self.context = self.runtime = self.lock = None
        self.external_browser = None
        self._wired = set()
        self.reused_auth = False
        self.expected_paper = None
        self.native_capture_active = False

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise PaperError("dependency_error", "Install requirements.txt and run python -m playwright install chromium") from None
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        self.lock = ProfileLock(self.root / "process.lock")
        self.lock.__enter__()
        try:
            self.runtime = sync_playwright().start()
            options = {"headless": not self.headed, "accept_downloads": True}
            if self.cfg.get("browser_channel"):
                options["channel"] = self.cfg["browser_channel"]
            if self.profile.get("external_browser"):
                from .external import connection
                self.external_browser = self.runtime.chromium.connect_over_cdp(connection(self.cfg, self.name))
                self.context = self.external_browser.contexts[0]
                self.reused_auth = True  # Fetch still validates actual PDF bytes.
            else:
                self.context = self.runtime.chromium.launch_persistent_context(str(self.root / "chromium"), **options)
            snapshot = self.root / "session-cookies.json"
            if snapshot.exists() and not self.external_browser:
                try:
                    self.context.add_cookies(json.loads(snapshot.read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    pass
            probe = self.root / "last-probe.json"
            if probe.exists():
                try:
                    self.reused_auth = self.reused_auth or json.loads(probe.read_text(encoding="utf-8")).get("status") in {"authenticated", "session_observed"}
                except (ValueError, OSError):
                    pass
            self.context.set_default_timeout(min(self.cfg["timeout"] * 1000, 10000))
            self.context.set_default_navigation_timeout(self.cfg["timeout"] * 1000)
            self.context.on("response", self._response)
            self.context.on("page", self._wire)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            for page in self.context.pages:
                self._wire(page)
            return self
        except PaperError:
            self.__exit__(None, None, None)
            raise
        except Exception:
            self.__exit__(None, None, None)
            raise PaperError("dependency_error", "Browser could not start; check Chromium installation, display, and profile permissions") from None

    def __exit__(self, *_):
        try:
            if self.context:
                try:
                    atomic_write(self.root / "session-cookies.json", json.dumps(self.context.cookies()).encode())
                except Exception:
                    pass
                if not self.external_browser:
                    self.context.close()
        finally:
            try:
                if self.runtime:
                    self.runtime.stop()
            finally:
                if self.lock:
                    self.lock.__exit__(None, None, None)

    def _wire(self, page):
        if id(page) in self._wired:
            return
        self._wired.add(id(page))
        page.on("download", self._download)

    def _download(self, download):
        if self.native_capture_active:
            return  # CDP owns this download; path() would block its event loop.
        try:
            path = download.path()
            if path and Path(path).stat().st_size <= self.cfg["max_bytes"] and len(self.captured) < 3:
                self.captured.append(Path(path).read_bytes())
        except Exception:
            pass

    def _response(self, response):
        try:
            if self.expected_paper and not matches_article(response.url, self.expected_paper):
                return
            if response.status == 429:
                self.blocked = {"host": urlsplit(response.url).hostname,
                                "retry_after": retry_seconds(response.headers.get("retry-after"))}
            if self.native_capture_active:
                return  # The response-stage interceptor owns body consumption.
            if response.status != 200 or "application/pdf" not in response.headers.get("content-type", "").lower():
                return
            if len(self.captured) >= 3 or int(response.headers.get("content-length") or 0) > self.cfg["max_bytes"]:
                return
            data = response.body()
            if len(data) <= self.cfg["max_bytes"]:
                self.captured.append(data)
        except Exception:
            pass

    def goto(self, url):
        if urlsplit(url).scheme not in {"http", "https"}:
            raise PaperError("configuration_error", "Browser target must be HTTP(S)")
        response = None
        try:
            response = self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            # Chromium aborts document navigation when it starts a PDF download.
            code = re.search(r"net::[A-Z_]+", str(exc))
            if not code or code.group(0) != "net::ERR_ABORTED":
                self._record("network_error", code.group(0) if code else "navigation_failed")
                raise PaperError("network_error", "Browser navigation failed", url=public_url(url),
                                 reason=code.group(0) if code else "navigation_failed") from None
        self.page.wait_for_timeout(400)
        if response and response.status == 403:
            challenge = bool(CHALLENGE.search(self._text()))
            status = "interaction_required" if challenge else "access_denied"
            self._record(status, "http_403")
            if not self.headed:
                raise PaperError(status, "Publisher returned HTTP 403 before authenticated access could be established", url=public_url(url))
        if response and response.status == 429:
            self._record("rate_limited", "http_429")
            raise PaperError("rate_limited", "Publisher requested cooldown", host=urlsplit(url).hostname,
                             retry_after=retry_seconds(response.headers.get("retry-after")))
        if response and response.status >= 500:
            self._record("network_error", "http_" + str(response.status))
            raise PaperError("network_error", "Publisher service returned HTTP " + str(response.status), url=public_url(url))

    def _current(self):
        pages = [p for p in self.context.pages if not p.is_closed()]
        if pages:
            self.page = pages[-1]
        return self.page

    def _text(self):
        try:
            return self.page.locator("body").inner_text(timeout=1500)[:60000]
        except Exception:
            return ""

    def _fill_login(self, attempted):
        # Credentials may only be sent to an explicit host for the matching frame.
        for frame in self.page.frames:
            if not host_matches(urlsplit(frame.url).hostname, self.profile.get("credential_hosts", [])):
                continue
            password = visible(frame, self.profile.get("password_selector", DEFAULT_PASSWORD))
            if password is None:
                continue
            key = (public_url(frame.url), "credentials")
            if key in attempted:
                continue
            user_env, pass_env = self.profile.get("username_env"), self.profile.get("password_env")
            username, secret = os.environ.get(user_env or ""), os.environ.get(pass_env or "")
            if not username or not secret:
                if self.headed:
                    return False  # Explicit interactive mode lets the user fill the official form.
                raise PaperError("auth_required", "Credential environment variables are not configured", profile=self.name)
            user = visible(frame, self.profile.get("username_selector", DEFAULT_USER))
            submit = visible(frame, self.profile.get("submit_selector", DEFAULT_SUBMIT))
            if user is None or submit is None:
                raise PaperError("auth_required", "Login form needs configured selectors", profile=self.name)
            attempted.add(key)  # one password submission per login URL per command
            user.fill(username)
            password.fill(secret)
            submit.click(timeout=5000)
            return True
        return False

    def _step(self, step, article_url):
        for frame in self.page.frames:
            if not host_matches(urlsplit(frame.url).hostname, step.get("hosts", [])):
                continue
            item = visible(frame, step.get("selector"))
            if item is None:
                continue
            if step["action"] == "fill":
                env = step.get("value_env")
                if env and not host_matches(urlsplit(frame.url).hostname, self.profile.get("credential_hosts", [])):
                    raise PaperError("configuration_error", "Secret fill requires a configured credential host")
                value = os.environ.get(env) if env else step.get("value", "")
                if value is None:
                    raise PaperError("auth_required", "A configured step environment variable is missing")
                item.fill(value)
            elif step["action"] == "click":
                item.click(timeout=5000)
            elif step["action"] == "press":
                item.press(step.get("key", "Enter"))
            else:
                raise PaperError("configuration_error", "Authentication steps support fill, click, or press")
            return True
        return False

    def authenticate(self, article_url):
        method = self.profile["method"]
        if method == "api":
            raise PaperError("configuration_error", "API profiles are used with fetch, not browser auth")
        target = article_url
        if method in {"webvpn", "ezproxy"}:
            template = self.profile.get("proxy_url_template")
            if not template:
                raise PaperError("configuration_error", f"{method} requires proxy_url_template; gateway URLs cannot be inferred")
            target = render_url(template, article_url)
        start = self.profile.get("entry_url") or target
        self.goto(start)
        if urlsplit(target).hostname in {"doi.org", "dx.doi.org"}:
            current_host = urlsplit(self.page.url).hostname
            if current_host and current_host not in {"doi.org", "dx.doi.org"} and not host_matches(current_host, self.profile.get("credential_hosts", [])):
                target = self.page.url
        deadline = time.monotonic() + self.cfg["browser_timeout"]
        attempted, completed_steps = set(), set()
        original_host = urlsplit(target).hostname
        saw_auth, gateway_opened, left_publisher = False, start == target, False
        while time.monotonic() < deadline:
            self._current()
            if self.blocked:
                raise PaperError("rate_limited", "Browser received a publisher cooldown", **self.blocked)
            if self.captured:
                self._record("session_observed", "pdf_response_observed")
                return {"status": "session_observed", "evidence": "pdf_response_observed", "profile": self.name, "target": target}
            text = self._text()
            if CHALLENGE.search(text) or visible(self.page, 'input[autocomplete="one-time-code"], input[name="captcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]') is not None:
                if self.headed:
                    self.page.wait_for_timeout(500)
                    continue
                raise PaperError("interaction_required", "Login requires a verification code or interactive challenge", profile=self.name)
            if NO_ACCESS.search(text):
                raise PaperError("not_entitled", "Publisher explicitly reports missing subscription access", profile=self.name)
            for index, step in enumerate(self.profile.get("steps", [])):
                if index not in completed_steps and self._step(step, article_url):
                    completed_steps.add(index)
                    saw_auth = True
                    self.page.wait_for_timeout(500)
                    break
            else:
                if self._fill_login(attempted):
                    saw_auth = True
                    self.page.wait_for_timeout(700)
                    continue
                # Only explicit evidence can mark an auth probe successful.
                evidence_selector = self.profile.get("success_selector")
                if evidence_selector and visible(self.page, evidence_selector) is not None:
                    self._record("authenticated", "configured_success_selector")
                    if not gateway_opened:
                        self.goto(target)
                    return {"status": "authenticated", "evidence": "configured_success_selector", "profile": self.name, "target": target}
                host = urlsplit(self.page.url).hostname
                left_publisher = left_publisher or host != original_host
                pdf_control = visible(self.page, 'a[href*="/pdfft"], a[href*="/pdf/"], a[href$=".pdf"]')
                has_meta = bool(self.page.locator('meta[name="citation_pdf_url"]').count())
                institution_link = visible(self.page, self.profile.get("institution_login_selector"))
                if institution_link is None:
                    for role in ("link", "button"):
                        items = self.page.get_by_role(role, name=LOGIN)
                        for i in range(min(items.count(), 8)):
                            if items.nth(i).is_visible():
                                institution_link = items.nth(i)
                                break
                        if institution_link is not None:
                            break
                interactive_evidence = not self.headed or self.reused_auth or saw_auth or left_publisher
                if host == original_host and interactive_evidence and (left_publisher or (institution_link is None and (pdf_control is not None or has_meta))) and visible(self.page, DEFAULT_PASSWORD) is None:
                    # This is a weak auth observation; fetch still verifies bytes.
                    self._record("session_observed", "publisher_return_or_pdf_control")
                    return {"status": "session_observed", "evidence": "publisher_return_or_pdf_control", "profile": self.name, "target": target}
                key = (public_url(self.page.url), "institution_link")
                if institution_link is not None and key not in attempted:
                    attempted.add(key)
                    institution_link.click(timeout=5000)
                    saw_auth = True
                    self.page.wait_for_timeout(600)
                    continue
                names = self.profile.get("institution_names", [])
                search = visible(self.page, self.profile.get("institution_search_selector",
                    '#searchInstitution, input[placeholder*="institution" i], input[placeholder*="organization" i], input[placeholder*="机构"], input[type="search"]'))
                if search is not None:
                    for name in names:
                        key = (public_url(self.page.url), "search:" + name)
                        if key not in attempted:
                            attempted.add(key)
                            search.fill(name)
                            self.page.wait_for_timeout(700)
                            break
                clicked = False
                for name in names:
                    key = (public_url(self.page.url), name)
                    if key in attempted:
                        continue
                    match = self.page.get_by_text(name, exact=True)
                    for i in range(min(match.count(), 8)):
                        if match.nth(i).is_visible():
                            attempted.add(key)
                            match.nth(i).click(timeout=4000)
                            clicked = saw_auth = True
                            break
                    if clicked:
                        break
                if clicked:
                    self.page.wait_for_timeout(600)
                    continue
                # A gateway login can finish without a visible success selector;
                # try the protected target once, which will redirect if still logged out.
                if not gateway_opened and saw_auth and visible(self.page, DEFAULT_PASSWORD) is None:
                    gateway_opened = True
                    self.goto(target)
                    continue
                if visible(self.page, DEFAULT_PASSWORD) is not None and not self.headed:
                    raise PaperError("auth_required", "Login did not complete automatically; check credentials, host rules, and selectors", profile=self.name)
                if not self.headed and not attempted and not self.profile.get("steps"):
                    break
                self.page.wait_for_timeout(500)
                continue
            self.page.wait_for_timeout(500)
        self._record("auth_required", "no_authentication_evidence")
        raise PaperError("auth_required", "No authenticated publisher session was established within the bounded login attempt", profile=self.name)

    def _record(self, status, evidence):
        atomic_write(self.root / "last-probe.json", json.dumps({"status": status, "evidence": evidence,
                     "checked_at": time.time(), "url": public_url(self.page.url)}, ensure_ascii=False).encode())

    def existing_pdf_candidates(self, paper):
        from .native_capture import capture
        for page in self.context.pages:
            if page.is_closed() or not matches_article(page.url, paper):
                continue
            if re.search(r"\.pdf$|/pdfft$", urlsplit(page.url).path, re.I):
                yield capture(self, paper, page.url)
                return

    def pdf_candidates(self, paper, target):
        from .native_capture import capture
        self.expected_paper = paper
        matching = next((p for p in self.context.pages if not p.is_closed() and public_url(p.url) == public_url(target)), None)
        if matching:
            self.page = matching
        elif public_url(self.page.url) != public_url(target):
            self.page = self.context.new_page()
            self.goto(target)
        route = publisher_config(self.cfg, self.page.url)
        try:
            self.page.wait_for_selector(route.get("pdf_selector") or 'meta[name="citation_pdf_url"], a[href*="/pdfft"], a[href$=".pdf"]', state="attached", timeout=min(5000, self.cfg["timeout"] * 1000))
        except Exception:
            pass
        markup = self.page.content()
        observed = parse_article(markup.encode(), self.page.url)
        if observed.get("doi") and paper.get("doi") and observed["doi"].lower() != paper["doi"].lower():
            raise PaperError("identity_unverified", "Current article metadata belongs to a different DOI")
        if observed.get("doi", "").lower() == paper.get("doi", "").lower() and observed.get("doi"):
            paper["article_url"] = self.page.url
        has_pdf_link = visible(self.page, route.get("pdf_selector") or 'a[href*="/pdfft"], a[href*="/pdf"], a[href$=".pdf"]') is not None
        has_institution_link = any(self.page.get_by_role(role, name=LOGIN).count() for role in ("link", "button"))
        if has_institution_link and not has_pdf_link:
            return
        urls = main_pdf_urls(markup, self.page.url, paper)
        if route.get("pdf_selector"):
            item = visible(self.page, route["pdf_selector"])
            if item is not None:
                href = item.get_attribute("href")
                actual = urljoin(self.page.url, href) if href else ""
                if actual and matches_article(actual, paper):
                    urls = [actual]
        for data in self.captured:
            if data.startswith(b"%PDF-") and b"%%EOF" in data[-4096:]:
                yield data
                return
        if urls:
            yield capture(self, paper, urls[0])


def auth_status(cfg, name):
    profile_for(cfg, name)
    root = Path(cfg["data_dir"]) / "profiles" / name
    probe = root / "last-probe.json"
    return {"status": "stored_state", "profile": name, "has_browser_profile": (root / "chromium").exists() or (root / "system-browser").exists(),
            "last_probe": json.loads(probe.read_text(encoding="utf-8")) if probe.exists() else None,
            "live_validated": False}
