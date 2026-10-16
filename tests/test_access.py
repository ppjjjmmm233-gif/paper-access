"""Offline tests, including a real Chromium SSO round trip against local fixtures."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import socket
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from paper_access.browser import Browser, auth_status
from paper_access.config import PaperError, host_matches, load_config
from paper_access.core import batch, fetch
from paper_access.metadata import normalize_doi
from paper_access.net import http_get, retry_seconds
from paper_access.storage import ProfileLock, Store, validate_pdf
from paper_access.external import endpoint_ready
from paper_access.routes import main_pdf_urls
from paper_access.native_capture import capture
from paper_access.reader import evaluate_target, save_existing_edge_reader, follow_native_download
from paper_access.routes import matches_article, pii_from_url

DOI = "10.5555/paper-access-test"
TITLE = "A reproducible paper access experiment"


def pdf(lines=None):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    lines = lines if lines is not None else [TITLE, DOI, "Full text of the test article."]
    commands = ["BT /F1 12 Tf 50 740 Td"]
    for line in lines:
        line = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.extend([f"({line}) Tj", "0 -20 Td"])
    commands.append("ET")
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


class Handler(BaseHTTPRequestHandler):
    calls = []
    login_posts = 0
    leak_headers = []

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass  # Chromium may close an idle fixture socket during shutdown.

    def send(self, body, kind="text/html", code=200, **headers):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind == "text/html" else ""))
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Handler.calls.append(self.path)
        path = urlsplit(self.path).path
        base = f"http://127.0.0.1:{self.server.server_port}"
        if path.startswith("/crossref"):
            item = {"DOI": DOI, "title": [TITLE], "URL": base + "/article"}
            message = {"items": [item]} if "?" in self.path else item
            return self.send(json.dumps({"message": message}), "application/json")
        if path.startswith("/openalex"):
            return self.send('{"locations": []}', "application/json")
        if path == "/public":
            return self.send(f'<meta name="citation_doi" content="{DOI}"><meta name="citation_title" content="{TITLE}"><meta name="citation_pdf_url" content="/paper.pdf">')
        if path == "/paper.pdf":
            return self.send(pdf(), "application/pdf")
        if path == "/attachment.pdf":
            return self.send(pdf(), "application/pdf", Content_Disposition='attachment; filename="paper.pdf"')
        if path == "/not-a-pdf":
            return self.send("A preparation page without a PDF")
        if path == "/observe.pdf":
            return self.send("A retained preparation page")
        if path == '/transient-denial.pdf':
            return self.send("<title>Preparing</title><script>setTimeout(() => location.href='/attachment.pdf', 250)</script>", code=403)
        if path == "/wrong.pdf":
            return self.send(pdf(["Different article", "10.5555/other"]), "application/pdf")
        if path == "/limited":
            return self.send("Slow down", code=429, Retry_After="120")
        if path == "/article":
            ticket = "ticket=ok" in self.path
            if ticket or "session=valid" in self.headers.get("Cookie", ""):
                return self.send(f'<p id="institution-access">Access provided by test institution</p><meta name="citation_doi" content="{DOI}"><a href="/paper.pdf">Download PDF</a>',
                                 **({"Set_Cookie": "session=valid; Path=/; HttpOnly"} if ticket else {}))
            return self.send(f'<meta name="citation_doi" content="{DOI}"><meta name="citation_title" content="{TITLE}"><meta name="citation_pdf_url" content="/restricted.pdf"><a href="/choose">Institutional login</a>')
        if path == "/choose":
            return self.send(f'<input type="search" placeholder="institution"><a href="http://localhost:{self.server.server_port}/idp">中国科技云通行证</a>')
        if path == "/english-article":
            return self.send('<a href="/choose-english">Institutional login</a>')
        if path == "/choose-english":
            return self.send(f'''<input type="search" placeholder="institution" oninput="document.querySelector('a').hidden = this.value !== 'CSTCloud ID (CAS)'"><a hidden href="http://localhost:{self.server.server_port}/idp">CSTCloud ID (CAS)</a>''')
        if path == "/idp":
            return self.send('<form method="post" action="/login"><input name="username" autocomplete="username"><input name="password" type="password"><button type="submit">登录</button></form>')
        if path == "/captcha":
            return self.send('<h1>Security verification</h1><input name="captcha">')
        if path == "/api-redirect":
            return self.send("", code=302, Location=base + "/leak")
        if path.startswith("/api/"):
            if self.headers.get("X-Secret") == "test-api-secret":
                return self.send(pdf(), "application/pdf")
            return self.send("Not authorized", code=401)
        if path == "/leak":
            Handler.leak_headers.append(self.headers.get("X-Secret"))
            return self.send(pdf(), "application/pdf")
        if path == "/not-entitled":
            return self.send("Your institution does not have access")
        if path == "/blocked":
            return self.send("There was a problem providing the content you requested CPE00001", code=403)
        return self.send("Not found", code=404)

    def do_POST(self):
        Handler.login_posts += 1
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        values = parse_qs(body)
        if values.get("username") == ["test-user"] and values.get("password") == ["test-secret"]:
            return self.send("", code=302, Location=f"http://127.0.0.1:{self.server.server_port}/article?ticket=ok")
        return self.send('<p>Invalid credentials</p><input type="password">')


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load_config()
        self.cfg.update(data_dir=str(self.root / "data"), timeout=4, browser_timeout=9,
                        delay_seconds=0, pdf_timeout=10, crossref_url=self.base + "/crossref", openalex_url=self.base + "/openalex")
        self.cfg["profiles"] = {"cstcloud": {"method": "cstcloud", "institution_names": ["中国科技云通行证"],
                     "credential_hosts": ["localhost"], "username_env": "TEST_PAPER_USER", "password_env": "TEST_PAPER_PASSWORD"}}
        self.output = self.root / "papers"
        Handler.calls, Handler.login_posts, Handler.leak_headers = [], 0, []

    def test_identifier_and_host_boundaries(self):
        self.assertEqual(normalize_doi("https://doi.org/10.5555/AbC"), "10.5555/abc")
        self.assertTrue(host_matches("a.passport.escience.cn", ["passport.escience.cn"]))
        self.assertFalse(host_matches("passport.escience.cn.evil.test", ["passport.escience.cn"]))

    def test_sciencedirect_main_route_excludes_reference_papers(self):
        main = 'S0022509626003273'
        other = 'S1359645403006748'
        article = 'https://www.sciencedirect.com/science/article/pii/' + main
        markup = f'<meta name="citation_pii" content="{main}"><a href="/science/article/pii/{other}/pdfft">Reference PDF</a><a href="/science/article/pii/{main}/pdfft?md5=real">View PDF</a>'
        urls = main_pdf_urls(markup, article, {"doi": DOI, "article_url": article})
        self.assertEqual(urls, [article + '/pdfft?md5=real'])

    def test_asset_path_uses_main_pii_after_issue_pii(self):
        url = 'https://pdf.sciencedirectassets.com/271504/1-s2.0-S0022509626X20099/1-s2.0-S0022509626003273/main.pdf'
        paper = {'article_url': 'https://www.sciencedirect.com/science/article/pii/S0022509626003273'}
        self.assertEqual(pii_from_url(url), 'S0022509626003273')
        self.assertTrue(matches_article(url, paper))
        self.assertFalse(matches_article(url.replace('S0022509626003273', 'S1359645403006748'), paper))

    def test_generic_doi_does_not_bind_another_publishers_reader(self):
        paper = {'doi':'10.1038/s41567-025-02862-4', 'article_url':'https://www.nature.com/articles/s41567-025-02862-4'}
        self.assertTrue(matches_article(paper['article_url']+'.pdf', paper))
        self.assertFalse(matches_article('https://www.sciencedirect.com/science/article/pii/S0022509626003273/pdfft', paper))
        self.assertFalse(matches_article('https://www.nature.com/articles/s41586-025-08772-y.pdf', paper))
        self.assertFalse(matches_article('https://www.nature.com/articles/s41567-025-02862-42.pdf', paper))
        aps = {'doi':'10.1103/PhysRevLett.119.161101', 'article_url':'https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.119.161101'}
        self.assertTrue(matches_article('https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.119.161101', aps))
        self.assertFalse(matches_article('https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.116.061102', aps))

    def test_cdp_target_evaluation_reads_existing_page_without_request(self):
        with Browser(self.cfg, 'cstcloud') as browser:
            browser.page.goto(self.base + '/public')
            control = browser.context.browser.new_browser_cdp_session()
            target = browser.context.new_cdp_session(browser.page)
            target_id = target.send('Target.getTargetInfo')['targetInfo']['targetId']
            before = list(Handler.calls)
            value = evaluate_target(control, browser.page, target_id, '({title:document.title, value:42})')
            self.assertEqual(value['value'], 42)
            self.assertEqual(Handler.calls, before)
            target.detach()
            control.detach()

    def test_reader_save_requires_exact_page_binding_and_persists_once(self):
        from unittest.mock import Mock
        control, page = Mock(), Mock()
        page.url = self.base + '/attachment.pdf'
        control.send.return_value = {'targetInfos': [{'type': 'webview', 'url':
            'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/edge_pdf/index.html', 'targetId': 'reader'}]}
        paper, state = {'article_url': self.base + '/article'}, {}
        checkpoint = Mock()
        with patch('paper_access.reader.evaluate_target', return_value={'url': self.base + '/other.pdf', 'ready': True}) as call:
            self.assertIsNone(save_existing_edge_reader(control, page, paper, state, checkpoint))
            self.assertEqual(call.call_count, 1)
            checkpoint.assert_not_called()
        import base64
        with patch('paper_access.reader.evaluate_target', side_effect=[{'url': page.url, 'ready': True}, base64.b64encode(pdf()).decode()]) as call:
            self.assertTrue(save_existing_edge_reader(control, page, paper, state, checkpoint).startswith(b'%PDF-'))
            self.assertTrue(state['reader_save_submitted'])
            save_existing_edge_reader(control, page, paper, state, checkpoint)
            self.assertEqual(call.call_count, 2)

    def test_native_resume_is_bound_to_guid_and_limited_to_one(self):
        from unittest.mock import Mock
        browser, page, checkpoint = Mock(), Mock(), Mock()
        browser.external_browser = True
        browser.cfg = dict(self.cfg, browser_channel='msedge')
        browser._downloads_refreshed = time.monotonic()
        page.url = 'edge://downloads/all'
        browser.context.pages = [page]
        state = {'guid': 'exact-download'}
        staging = self.root / 'staging'
        paper = {'article_url': 'https://www.sciencedirect.com/science/article/pii/S0022509626003273'}
        item = {'id': '7', 'state': 'interrupted', 'details': '无法下载 - 网络问题',
                'canResume': True, 'path': str(staging / state['guid'])}
        page.evaluate.side_effect = [[dict(item, path=str(staging / 'other-download'))], [item], True, [item]]
        follow_native_download(browser, state, staging, checkpoint, paper)
        self.assertNotIn('resumes', state)
        follow_native_download(browser, state, staging, checkpoint, paper)
        self.assertEqual(state['resumes'], 1)
        follow_native_download(browser, state, staging, checkpoint, paper)
        self.assertEqual(page.evaluate.call_count, 4, 'Only one call may dispatch Resume')

    def test_native_resume_honors_publisher_cooldown(self):
        from unittest.mock import Mock
        browser, page = Mock(), Mock()
        browser.external_browser = True
        browser.cfg = dict(self.cfg, browser_channel='msedge')
        browser._downloads_refreshed = time.monotonic()
        page.url = 'edge://downloads/all'
        browser.context.pages = [page]
        state = {'guid': 'exact-download'}
        staging = self.root / 'staging'
        page.evaluate.return_value = [{'id': '7', 'state': 'interrupted', 'details': '网络问题',
            'canResume': True, 'path': str(staging / state['guid'])}]
        store = Store(self.cfg['data_dir'])
        store.cooldown('sciencedirect.com', 120)
        store.close()
        follow_native_download(browser, state, staging, Mock(), {'article_url': 'https://www.sciencedirect.com/article'})
        self.assertNotIn('resumes', state)
        self.assertEqual(page.evaluate.call_count, 1)

    def test_external_profile_cooldown_makes_no_publisher_requests(self):
        self.cfg['profiles']['cstcloud']['external_browser'] = True
        store = Store(self.cfg['data_dir'])
        store.cooldown('www.sciencedirect.com', 120)
        self.assertGreater(store.cooldown('pdf.sciencedirectassets.com'), 110)
        store.close()
        with patch('paper_access.core.Browser') as browser, patch('paper_access.core.http_get') as http:
            result = fetch(self.cfg, self.output, doi=DOI, url='https://www.sciencedirect.com/science/article/pii/S0022509626003273', profile='cstcloud')
        self.assertEqual(result['status'], 'rate_limited')
        browser.assert_not_called()
        http.assert_not_called()

    def test_native_capture_submits_one_navigation_and_reuses_complete_file(self):
        paper = {'doi': DOI, 'title': TITLE, 'article_url': self.base + '/article'}
        with Browser(self.cfg, 'cstcloud') as browser:
            browser.expected_paper = paper
            data = capture(browser, paper, self.base + '/attachment.pdf')
            self.assertEqual(validate_pdf(data, paper)['identity_evidence'], 'doi')
            before = len(Handler.calls)
            again = capture(browser, paper, self.base + '/attachment.pdf')
            self.assertEqual(again, data)
            self.assertEqual(len(Handler.calls), before)
        self.assertEqual(sum(u == '/attachment.pdf' for u in Handler.calls), 1)

    def test_native_capture_observes_success_after_transient_denial(self):
        paper = {'doi': DOI, 'article_url': self.base + '/article'}
        with Browser(self.cfg, 'cstcloud') as browser:
            browser.expected_paper = paper
            data = capture(browser, paper, self.base + '/transient-denial.pdf')
            self.assertEqual(validate_pdf(data, paper)['identity_evidence'], 'doi')
        self.assertEqual(Handler.calls.count('/transient-denial.pdf'), 1)
        self.assertEqual(Handler.calls.count('/attachment.pdf'), 1)

    def test_native_pending_attempt_is_not_resubmitted(self):
        self.cfg['pdf_timeout'] = 1
        paper = {'doi': DOI, 'article_url': self.base + '/article'}
        with Browser(self.cfg, 'cstcloud') as browser:
            with self.assertRaises(PaperError) as err:
                capture(browser, paper, self.base + '/not-a-pdf')
            self.assertEqual(err.exception.result['status'], 'download_pending')
            before = len(Handler.calls)
            with self.assertRaises(PaperError):
                capture(browser, paper, self.base + '/not-a-pdf')
            self.assertEqual(len(Handler.calls), before)

    def test_native_resume_observes_existing_reader_without_navigation(self):
        self.cfg['pdf_timeout'] = 0.3
        self.cfg['browser_timeout'] = 0.3
        paper = {'doi': DOI, 'article_url': self.base + '/article'}
        with Browser(self.cfg, 'cstcloud') as browser:
            with self.assertRaises(PaperError):
                capture(browser, paper, self.base + '/observe.pdf')
            before = sum(u == '/observe.pdf' for u in Handler.calls)
            with self.assertRaises(PaperError):
                capture(browser, paper, self.base + '/observe.pdf')
            self.assertEqual(sum(u == '/observe.pdf' for u in Handler.calls), before)

    def test_pdf_identity_and_html_rejection(self):
        self.assertEqual(validate_pdf(pdf(), {"doi": DOI})["pages"], 1)
        for data in (b"<html>login</html>", pdf(["Other", DOI + "2"]), pdf(["Supporting Information", DOI])):
            with self.assertRaises(PaperError):
                validate_pdf(data, {"doi": DOI})

    def test_title_search_does_not_download(self):
        result = fetch(self.cfg, self.output, title=TITLE)
        self.assertEqual(result["status"], "candidates")
        self.assertFalse(list(self.output.glob("*.pdf")))

    def test_public_download_cache_copy_and_corruption(self):
        result = fetch(self.cfg, self.output, url=self.base + "/public")
        self.assertEqual(result["status"], "downloaded")
        Handler.calls = []
        cached = fetch(self.cfg, self.root / "other", doi=DOI)
        self.assertEqual(cached["status"], "cached")
        self.assertEqual(Handler.calls, [])
        self.assertTrue(Path(cached["file"]).with_suffix(".json").exists())
        Path(result["file"]).write_bytes(b"broken")
        repaired = fetch(self.cfg, self.output, doi=DOI, url=self.base + "/public")
        self.assertEqual(repaired["status"], "downloaded")

    def test_batch_continues_after_invalid_input_and_deduplicates(self):
        fetch(self.cfg, self.output, url=self.base + "/public")
        filename = self.root / "papers.txt"
        filename.write_text(f"invalid\n{DOI}\n{DOI}\n", encoding="utf-8")
        result = batch(self.cfg, filename, self.output)
        self.assertEqual((result["total"], result["succeeded"], result["failed"]), (2, 1, 1))
        self.assertEqual(result["results"][1]["status"], "cached")

    def test_retry_after_and_persisted_cooldown(self):
        self.assertEqual(retry_seconds("120"), 120)
        with self.assertRaises(PaperError) as err:
            http_get(self.base + "/limited", self.cfg)
        self.assertEqual(err.exception.result["retry_after"], 120)
        store = Store(self.cfg["data_dir"])
        store.cooldown("127.0.0.1", 120)
        store.close()
        store = Store(self.cfg["data_dir"])
        self.assertGreater(store.cooldown("127.0.0.1"), 110)
        store.close()

    def test_api_does_not_forward_credentials_on_redirect(self):
        with self.assertRaises(PaperError):
            http_get(self.base + "/api-redirect", self.cfg, {"X-Secret": "secret"}, allow_redirects=False)
        self.assertEqual(Handler.leak_headers, [])

    def test_process_lock_rejects_competing_owner(self):
        with ProfileLock(self.root / "process.lock"):
            with self.assertRaises(PaperError) as err:
                with ProfileLock(self.root / "process.lock"):
                    pass
            self.assertEqual(err.exception.result["status"], "profile_busy")

    def test_api_profile_downloads_using_environment_header(self):
        self.cfg["profiles"]["api"] = {"method": "api", "url_template": self.base + "/api/{doi}",
                                         "headers_env": {"X-Secret": "TEST_API_KEY"}}
        with patch.dict(os.environ, {"TEST_API_KEY": "test-api-secret"}):
            result = fetch(self.cfg, self.output, doi=DOI, url=self.base + "/article", profile="api")
        self.assertEqual(result["status"], "downloaded", result)
        self.assertNotIn("test-api-secret", json.dumps(result))

    def test_cli_json_failure_from_another_working_directory(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "paper.py"
        result = subprocess.run([sys.executable, str(script), "fetch"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["status"], "configuration_error")
        result = subprocess.run([sys.executable, str(script), "--unsupported"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["status"], "configuration_error")

    def test_browser_cstcloud_login_download_and_session_reuse(self):
        with patch.dict(os.environ, {"TEST_PAPER_USER": "test-user", "TEST_PAPER_PASSWORD": "test-secret"}):
            result = fetch(self.cfg, self.output, doi=DOI, url=self.base + "/article", profile="cstcloud")
            self.assertEqual(result["status"], "downloaded", result)
            self.assertEqual(Handler.login_posts, 1)
            with Browser(self.cfg, "cstcloud") as browser:
                probe = browser.authenticate(self.base + "/article")
                self.assertIn(probe["status"], {"session_observed", "authenticated"})
            self.assertEqual(Handler.login_posts, 1)
            self.assertFalse(auth_status(self.cfg, "cstcloud")["live_validated"])

    def test_browser_does_not_fill_an_unapproved_identity_host(self):
        self.cfg["profiles"]["cstcloud"]["credential_hosts"] = ["not-localhost.test"]
        with patch.dict(os.environ, {"TEST_PAPER_USER": "test-user", "TEST_PAPER_PASSWORD": "test-secret"}):
            with Browser(self.cfg, "cstcloud") as browser:
                with self.assertRaises(PaperError):
                    browser.authenticate(self.base + "/idp")
        self.assertEqual(Handler.login_posts, 0)

    def test_browser_search_falls_back_to_english_institution_name(self):
        self.cfg["profiles"]["cstcloud"]["institution_names"].append("CSTCloud ID (CAS)")
        with patch.dict(os.environ, {"TEST_PAPER_USER": "test-user", "TEST_PAPER_PASSWORD": "test-secret"}):
            with Browser(self.cfg, "cstcloud") as browser:
                result = browser.authenticate(self.base + "/english-article")
                self.assertIn(result["status"], {"session_observed", "authenticated"})
                self.assertEqual(Handler.login_posts, 1)

    def test_browser_challenge_and_entitlement_are_distinct(self):
        for path, expected in (("/captcha", "interaction_required"), ("/not-entitled", "not_entitled")):
            with Browser(self.cfg, "cstcloud") as browser:
                with self.assertRaises(PaperError) as err:
                    browser.authenticate(self.base + path)
                self.assertEqual(err.exception.result["status"], expected)

    def test_browser_http_403_is_not_reported_as_missing_login(self):
        with Browser(self.cfg, "cstcloud") as browser:
            with self.assertRaises(PaperError) as err:
                browser.authenticate(self.base + "/blocked")
            self.assertEqual(err.exception.result["status"], "access_denied")

    def test_explicit_interactive_mode_keeps_missing_credentials_for_user(self):
        with Browser(self.cfg, "cstcloud") as browser:
            browser.headed = True
            browser.goto(self.base.replace("127.0.0.1", "localhost") + "/idp")
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(browser._fill_login(set()))
            self.assertEqual(Handler.login_posts, 0)

    def test_known_doi_can_reach_browser_when_http_article_is_blocked(self):
        result = fetch(self.cfg, self.output, doi=DOI, url=self.base + "/blocked", profile="cstcloud")
        self.assertEqual(result["status"], "access_denied", result)
        self.assertEqual(result["url"], self.base + "/blocked")

    def test_browser_gateway_and_carsi_methods(self):
        for method in ("carsi", "publisher", "webvpn", "ezproxy"):
            with self.subTest(method=method):
                name = "fixture-" + method
                profile = dict(self.cfg["profiles"]["cstcloud"], method=method)
                if method in {"publisher", "webvpn", "ezproxy"}:
                    profile["entry_url"] = self.base.replace("127.0.0.1", "localhost") + "/idp"
                if method in {"webvpn", "ezproxy"}:
                    profile["proxy_url_template"] = "{raw_url}"
                self.cfg["profiles"][name] = profile
                with patch.dict(os.environ, {"TEST_PAPER_USER": "test-user", "TEST_PAPER_PASSWORD": "test-secret"}):
                    with Browser(self.cfg, name) as browser:
                        probe = browser.authenticate(self.base + "/article")
                        candidates = list(browser.pdf_candidates({"doi": DOI, "article_url": self.base + "/article"}, probe["target"]))
                        self.assertTrue(candidates)
                        self.assertEqual(validate_pdf(candidates[0], {"doi": DOI})["identity_evidence"], "doi")

    def test_external_browser_attachment_reuses_session_and_leaves_window_running(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            binary = runtime.chromium.executable_path
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen([binary, "--headless=new", "--no-first-run", "--remote-debugging-address=127.0.0.1",
                                    "--remote-debugging-port=" + str(port), "--user-data-dir=" + str(self.root / "external"), "about:blank"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        self.addCleanup(cleanup)
        for _ in range(40):
            if endpoint_ready(port):
                break
            time.sleep(0.2)
        self.assertTrue(endpoint_ready(port))
        root = Path(self.cfg["data_dir"]) / "profiles" / "cstcloud"
        root.mkdir(parents=True)
        (root / "browser-connection.json").write_text(json.dumps({"port": port}), encoding="utf-8")
        self.cfg["profiles"]["cstcloud"]["external_browser"] = True
        with patch.dict(os.environ, {"TEST_PAPER_USER": "test-user", "TEST_PAPER_PASSWORD": "test-secret"}):
            result = fetch(self.cfg, self.output, doi=DOI, url=self.base + "/article", profile="cstcloud")
        self.assertEqual(result["status"], "downloaded", result)
        self.assertIsNone(process.poll(), "Disconnect must preserve the user's dedicated browser")


if __name__ == "__main__":
    unittest.main()
