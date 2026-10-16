"""One browser navigation, capture its actual response or native download."""
import base64
import hashlib
import json
import re
import sys
from pathlib import Path
import time
from urllib.parse import urlsplit

from .config import PaperError
from .net import retry_seconds
from .routes import matches_article
from .storage import atomic_write
from .reader import save_existing_edge_reader, follow_native_download


def capture(browser, paper, url):
    if not matches_article(url, paper):
        raise PaperError("identity_unverified", "PDF URL belongs to a different article")
    key = hashlib.sha256((paper.get("doi") or paper["article_url"]).encode()).hexdigest()[:24]
    staging = browser.root / "downloads" / key
    staging.mkdir(parents=True, exist_ok=True)
    checkpoint = staging / "attempt.json"
    previous = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    if previous.get("file") == "captured.pdf" and (staging / "captured.pdf").is_file():
        return (staging / "captured.pdf").read_bytes()
    if previous.get("guid"):
        path = staging / previous["guid"]
        if path.is_file() and path.stat().st_size <= browser.cfg["max_bytes"]:
            return path.read_bytes()
    resume_page = None
    if previous.get("submitted"):
        resume_page = next((p for p in browser.context.pages if not p.is_closed() and matches_article(p.url, paper)
                            and re.search(r"\.pdf$|/pdfft$|/pdfdirect/", urlsplit(p.url).path, re.I)), None)
        if resume_page is None:
            raise PaperError("download_pending", "A native save was already submitted; inspect its existing record before retrying", profile=browser.name)

    state = previous if resume_page else {"submitted": False, "doi": paper.get("doi"), "state": "prepared"}
    def save():
        atomic_write(checkpoint, json.dumps(state).encode())
    page = resume_page or browser.context.new_page()
    session = browser.context.new_cdp_session(page)
    owner = browser.external_browser or browser.context.browser
    control = owner.new_browser_cdp_session()
    candidates, completed, blockers, intercepted = {}, [], [], []
    initial_captured = len(browser.captured)

    def response_received(event):
        response = event.get("response", {})
        target = response.get("url", "")
        if not matches_article(target, paper):
            return
        status = int(response.get("status", 0))
        headers = {k.lower(): str(v) for k, v in response.get("headers", {}).items()}
        if status in {401, 403, 429}:
            blockers.append({"status": status, "host": urlsplit(target).hostname,
                             "retry_after": retry_seconds(headers.get("retry-after"))})
        mime = response.get("mimeType", "").lower()
        if status in {200, 206} and ("pdf" in mime or "octet-stream" in mime):
            candidates[event["requestId"]] = response

    def finished(event):
        if event.get("requestId") in candidates:
            completed.append(event["requestId"])

    def download_begin(event):
        if matches_article(event.get("url", ""), paper) and not state.get('guid'):
            state.update(guid=event["guid"], state="downloading")
            save()

    def request_paused(event):
        request_id = event["requestId"]
        target = event.get("request", {}).get("url", "")
        headers = event.get("responseHeaders", [])
        lookup = {h["name"].lower(): h["value"] for h in headers}
        status = event.get("responseStatusCode", 0)
        if matches_article(target, paper) and status in {401, 403, 429}:
            blockers.append({"status": status, "host": urlsplit(target).hostname,
                             "retry_after": retry_seconds(lookup.get("retry-after"))})
        if matches_article(target, paper) and status == 200 and "pdf" in lookup.get("content-type", "").lower():
            try:
                body = session.send("Fetch.getResponseBody", {"requestId": request_id})
                data = base64.b64decode(body["body"]) if body.get("base64Encoded") else body["body"].encode()
                forwarded = [h for h in headers if h["name"].lower() not in {"content-length", "content-encoding", "transfer-encoding"}]
                forwarded.append({"name": "Content-Length", "value": str(len(data))})
                session.send("Fetch.fulfillRequest", {"requestId": request_id, "responseCode": status,
                             "responseHeaders": forwarded, "body": base64.b64encode(data).decode()})
                if data.startswith(b"%PDF-") and b"%%EOF" in data[-4096:] and len(data) <= browser.cfg["max_bytes"]:
                    intercepted.append(data)
                return
            except Exception:
                pass
        try:
            session.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception:
            pass

    session.on("Network.responseReceived", response_received)
    session.on("Network.loadingFinished", finished)
    session.on("Fetch.requestPaused", request_paused)
    control.on("Browser.downloadWillBegin", download_begin)
    browser.native_capture_active = True
    try:
        session.send("Network.enable", {"maxTotalBufferSize": int(browser.cfg["max_bytes"] * 2),
                                       "maxResourceBufferSize": int(browser.cfg["max_bytes"])})
        session.send("Page.enable")
        session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
        control.send("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": str(staging.resolve()), "eventsEnabled": True})
        if resume_page:
            state.update(state="observing_existing_reader", resumed_at=time.time())
            save()
            print(json.dumps({"event": "observing_existing_reader", "profile": browser.name,
                              "message": "No new PDF navigation submitted; complete any visible verification in the retained reader"}), file=sys.stderr, flush=True)
        else:
            state.update(submitted=True, state="submitted", submitted_at=time.time())
            save()  # Persist before the only publisher PDF navigation.
            session.send("Page.navigate", {"url": url, "referrer": paper["article_url"], "referrerPolicy": "strictOriginWhenCrossOrigin"})
        deadline = time.monotonic() + (browser.cfg["browser_timeout"] if resume_page else browser.cfg.get("pdf_timeout", 60))
        seen = set()
        reader_checked = 0
        deferred_denial = None
        while time.monotonic() < deadline:
            page.wait_for_timeout(150)
            if deferred_denial and time.monotonic() >= deferred_denial[1]:
                blockers.append({**deferred_denial[0], 'grace_elapsed': True})
                deferred_denial = None
            if blockers:
                blocked = blockers.pop(0)
                if blocked["status"] == 429:
                    state["state"] = "rate_limited"
                    save()
                    raise PaperError("rate_limited", "Publisher requested cooldown", host=blocked["host"], retry_after=blocked["retry_after"])
                text = page.title()
                try:
                    text += page.locator("body").inner_text(timeout=1000)[:3000]
                except Exception:
                    pass
                if blocked["status"] == 403 and (resume_page is not None or re.search(r"just a moment|请稍候|verify.*human|security verification|checking your browser", text, re.I)):
                    if state.get("state") != "interaction_required":
                        state["state"] = "interaction_required"
                        save()
                        print(json.dumps({"event": "interaction_required", "profile": browser.name,
                                          "message": "Complete the official verification in the retained PDF reader"}), file=sys.stderr, flush=True)
                        if resume_page is None:
                            deadline = max(deadline, time.monotonic() + browser.cfg["browser_timeout"])
                    continue
                if blocked['status'] == 403 and not blocked.get('grace_elapsed'):
                    # A challenge response may be followed by the successful
                    # browser response. Observe it briefly without re-navigation.
                    if deferred_denial is None:
                        deferred_denial = (blocked, time.monotonic() + 5)
                    continue
                if intercepted:
                    # A complete successful response supersedes the earlier 403.
                    blockers.clear()
                    continue
                state["state"] = "auth_required" if blocked["status"] == 401 else "access_denied"
                save()
                raise PaperError(state["state"], "Publisher blocked the single PDF navigation")
            if time.monotonic() - reader_checked > 2 and not state.get('reader_save_submitted'):
                reader_checked = time.monotonic()
                reader_data = save_existing_edge_reader(control, page, paper, state, save)
                if reader_data:
                    intercepted.append(reader_data)
            for data in intercepted + browser.captured[initial_captured:]:
                if data.startswith(b"%PDF-") and b"%%EOF" in data[-4096:] and len(data) <= browser.cfg["max_bytes"]:
                    atomic_write(staging / "captured.pdf", data)
                    state.update(state="captured", file="captured.pdf")
                    save()
                    return data
            for request in completed:
                if request in seen:
                    continue
                seen.add(request)
                try:
                    body = session.send("Network.getResponseBody", {"requestId": request})
                    data = base64.b64decode(body["body"]) if body.get("base64Encoded") else body["body"].encode()
                    if data.startswith(b"%PDF-") and b"%%EOF" in data[-4096:] and len(data) <= browser.cfg["max_bytes"]:
                        atomic_write(staging / "captured.pdf", data)
                        state.update(state="captured", file="captured.pdf")
                        save()
                        return data
                except Exception:
                    pass
            if state.get("guid"):
                path = staging / state["guid"]
                if path.is_file() and path.stat().st_size <= browser.cfg["max_bytes"]:
                    data = path.read_bytes()
                    if data.startswith(b"%PDF-") and b"%%EOF" in data[-4096:]:
                        state["state"] = "downloaded"
                        save()
                        return data
                if time.monotonic() - reader_checked > 2:
                    reader_checked = time.monotonic()
                    data = follow_native_download(browser, state, staging, save, paper)
                    if data:
                        return data
        state["state"] = "capture_unavailable"
        save()
        raise PaperError("download_pending", "PDF navigation was submitted once; no complete response/file was captured. The reader and save record are preserved")
    finally:
        browser.native_capture_active = False
        try:
            try:
                session.send("Fetch.disable")
            finally:
                control.send("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": False})
        finally:
            session.detach()
            control.detach()
