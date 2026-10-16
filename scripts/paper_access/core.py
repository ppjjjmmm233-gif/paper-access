import csv
import importlib.util
import json
import os
from pathlib import Path
import time
from urllib.parse import quote, urlsplit

from .browser import Browser, render_url
from .config import PaperError, profile_for
from .metadata import normalize_doi, oa_locations, parse_article, resolve
from .net import http_get
from .storage import ProfileLock, Store


def _attempt(cfg, store, url, headers=None, allow_redirects=True):
    host = urlsplit(url).hostname or ""
    cooldown = store.cooldown(host)
    if cooldown:
        raise PaperError("rate_limited", "Persisted publisher cooldown is active", host=host, retry_after=cooldown)
    store.space_request(host, cfg["delay_seconds"])
    try:
        return http_get(url, cfg, headers, allow_redirects=allow_redirects)
    except PaperError as exc:
        if exc.result["status"] == "rate_limited":
            store.cooldown(exc.result.get("host") or host, exc.result["retry_after"])
        raise


def _api(cfg, store, paper, profile):
    if not paper.get("doi"):
        raise PaperError("invalid_identifier", "API download requires a DOI")
    template = profile.get("url_template")
    if not template:
        raise PaperError("configuration_error", "API profile requires url_template")
    headers = {"Accept": "application/pdf", **profile.get("headers", {})}
    for header, env in profile.get("headers_env", {}).items():
        value = os.environ.get(env)
        if not value:
            raise PaperError("configuration_error", "An API credential environment variable is missing")
        headers[header] = value
    # API requests do not follow redirects: never forward credentials to another host.
    return _attempt(cfg, store, render_url(template, paper["article_url"], paper["doi"]), headers, allow_redirects=False)[0]


def _browser_download(cfg, store, paper, output, profile, headed, attempts):
    details = profile_for(cfg, profile)
    host = urlsplit(paper["article_url"]).hostname or ""
    seconds = store.cooldown(host)
    if seconds:
        raise PaperError("rate_limited", "Persisted publisher cooldown is active", host=host, retry_after=seconds)
    store.space_request(host, cfg["delay_seconds"])
    with Browser(cfg, profile, headed) as browser:
        browser.expected_paper = paper
        # An existing dedicated article is authoritative for metadata, and avoids
        # probing its paywall through a different HTTP client.
        for page in browser.context.pages:
            if page.is_closed() or page.url.split("?", 1)[0] != paper["article_url"].split("?", 1)[0]:
                continue
            parsed = parse_article(page.content().encode(), page.url)
            if parsed.get("doi", "").lower() == paper.get("doi", "").lower():
                paper["title"] = paper.get("title") or parsed.get("title", "")
            break
        for data in browser.existing_pdf_candidates(paper):
            return store.save(data, paper, output, details["method"] + ":" + profile + ":open_pdf")
        for data in browser.pdf_candidates(paper, paper["article_url"]):
            return store.save(data, paper, output, details["method"] + ":" + profile + ":native_capture")
        auth = browser.authenticate(paper["article_url"])
        for data in browser.pdf_candidates(paper, auth["target"]):
            return store.save(data, paper, output, details["method"] + ":" + profile + ":native_capture")
    raise PaperError("download_unavailable", "No article-bound PDF route was available")


def _fetch(cfg, store, output, doi=None, title=None, url=None, profile=None, oa_only=False, headed=False):
    if profile:
        profile_for(cfg, profile)
    if doi:
        doi = normalize_doi(doi)
        cached = store.cached({"doi": doi, "title": title or ""}, output)
        if cached:
            return cached
    if doi and url and profile and not oa_only and profile_for(cfg, profile).get("external_browser"):
        paper = {"doi": doi, "title": title or "", "article_url": url}
        try:
            return _browser_download(cfg, store, paper, output, profile, headed, [])
        except PaperError as exc:
            if exc.result["status"] == "rate_limited":
                store.cooldown(exc.result.get("host") or urlsplit(url).hostname, exc.result["retry_after"])
            return {**exc.result, "doi": doi}
    paper = resolve(cfg, doi, title, url)
    if paper.get("status") == "candidates":
        return paper
    cached = store.cached(paper, output)
    if cached:
        return cached
    attempts = []
    # A public article page can advertise its OA PDF even without OpenAlex coverage.
    if not paper.get("pdf_urls"):
        try:
            data, final, _ = _attempt(cfg, store, paper["article_url"])
            article = parse_article(data, final)
            paper.update(article_url=final, pdf_urls=article["pdf_urls"])
            if not paper.get("title"):
                paper["title"] = article["title"]
        except PaperError as exc:
            attempts.append({"source": "article_page", **exc.result})
    for location in oa_locations(paper, cfg):
        try:
            data, _, _ = _attempt(cfg, store, location["url"], {"Accept": "application/pdf"})
            return store.save(data, paper, output, location["source"], location["version"])
        except PaperError as exc:
            attempts.append({"source": location["source"], **exc.result})
    if profile and not oa_only:
        details = profile_for(cfg, profile)
        host = urlsplit(paper["article_url"]).hostname or ""
        try:
            if details["method"] == "api":
                data = _api(cfg, store, paper, details)
                return store.save(data, paper, output, "api:" + profile)
            return _browser_download(cfg, store, paper, output, profile, headed, attempts)
        except PaperError as exc:
            if exc.result["status"] == "rate_limited":
                store.cooldown(exc.result.get("host") or host, exc.result["retry_after"])
            return {**exc.result, "doi": paper.get("doi"), "attempts": attempts}
    # Preserve a specific validation/cooldown failure instead of a misleading paywall result.
    failures = [a for a in attempts if a["status"] in {"rate_limited", "invalid_pdf", "identity_unverified"}]
    if failures:
        return {**failures[-1], "doi": paper.get("doi"), "attempts": attempts}
    return {"status": "download_unavailable" if oa_only else "auth_required", "doi": paper.get("doi"),
            "message": "No verified public PDF found" + ("; select an institutional profile" if not oa_only else ""),
            "attempts": attempts}


def fetch(cfg, output, **kwargs):
    # This lock also prevents competing agents from racing the same output/cache.
    with ProfileLock(Path(cfg["data_dir"]) / "retrieval.lock"):
        store = Store(cfg["data_dir"])
        try:
            return _fetch(cfg, store, output, **kwargs)
        finally:
            store.close()


def batch(cfg, filename, output, **kwargs):
    path = Path(filename)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "doi" not in reader.fieldnames:
                raise PaperError("configuration_error", "CSV requires a doi column")
            rows = [{"doi": row.get("doi", "").strip(), "title": (row.get("title") or "").strip()} for row in reader]
    else:
        rows = [{"doi": line.strip()} for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    results, seen = [], set()
    with ProfileLock(Path(cfg["data_dir"]) / "retrieval.lock"):
        store = Store(cfg["data_dir"])
        try:
            for row in rows:
                try:
                    doi = normalize_doi(row["doi"])
                    if doi in seen:
                        continue
                    seen.add(doi)
                    if results and cfg["delay_seconds"]:
                        time.sleep(cfg["delay_seconds"])
                    results.append(_fetch(cfg, store, output, **row, **kwargs))
                except PaperError as exc:
                    results.append({**exc.result, "doi": row.get("doi")})
        finally:
            store.close()
    succeeded = sum(r["status"] in {"downloaded", "cached"} for r in results)
    return {"status": "completed" if succeeded == len(results) else "partial", "total": len(results),
            "succeeded": succeeded, "failed": len(results) - succeeded, "results": results}


def doctor(cfg):
    modules = {m: importlib.util.find_spec(m) is not None for m in ("pypdf", "playwright")}
    browser_installed = False
    if modules["playwright"]:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as runtime:
                browser_installed = Path(runtime.chromium.executable_path).exists()
        except Exception:
            pass
    return {"status": "ready" if all(modules.values()) and browser_installed else "setup_required",
            "dependencies": modules, "chromium_installed": browser_installed, "data_dir": cfg["data_dir"],
            "profiles": {k: {"method": p["method"],
                              "credentials_configured": all(os.environ.get(p.get(key, "")) for key in ("username_env", "password_env"))
                              if p["method"] != "api" else all(os.environ.get(v) for v in p.get("headers_env", {}).values())}
                         for k, p in cfg["profiles"].items()}}
