"""Bind publisher PDF routes to the requested article before any request."""
import html
import json
import re
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from .config import host_matches
from .metadata import CitationHTML


def pii_from_url(url):
    matches = list(re.finditer(r"(?:/pii/|1-s2\.0-)([A-Z0-9]{16,17})(?=[/?#.-]|$)", unquote(url), re.I))
    # Asset paths contain an issue PII before the main-document PII.
    return matches[-1].group(1).upper() if matches else ""


def publisher_key(host):
    host = (host or "").lower()
    return "sciencedirect.com" if host_matches(host, ["sciencedirect.com", "sciencedirectassets.com"]) else host


def matches_article(url, paper):
    candidate, article = urlsplit(url), urlsplit(paper.get("article_url", ""))
    pii = paper.get("pii") or pii_from_url(paper.get("article_url", ""))
    if pii:
        allowed_host = candidate.hostname == article.hostname or (
            publisher_key(candidate.hostname) == publisher_key(article.hostname) == "sciencedirect.com")
        return bool(allowed_host and pii_from_url(url) == pii.upper())
    doi = paper.get('doi', '').lower()
    if host_matches(article.hostname, ['nature.com']) and doi.startswith('10.1038/'):
        token = doi.split('/', 1)[1]
        return bool(candidate.hostname == article.hostname and re.fullmatch(
            r'/articles/' + re.escape(token) + r'(?:\.pdf)?/?', unquote(candidate.path), re.I))
    if host_matches(article.hostname, ['aps.org']) and doi.startswith('10.1103/'):
        return bool(candidate.hostname == article.hostname and doi in unquote(candidate.path).lower())
    # A generic DOI must never bind an unrelated publisher's retained reader.
    return candidate.scheme in {"http", "https"} and candidate.hostname == article.hostname


def main_pdf_urls(markup, article_url, paper):
    parser = CitationHTML(article_url)
    parser.feed(markup)
    pii = pii_from_url(article_url) or parser.meta.get("citation_pii", "").upper()
    if pii:
        paper["pii"] = pii
    urls = []
    # Read the publisher's own structured PDF route; do not invent a DOI/PII.
    for marker in re.finditer(r'"pdfDownload"\s*:\s*', markup):
        try:
            obj, _ = json.JSONDecoder().raw_decode(markup[marker.end():])
            meta = obj.get("urlMetadata", {})
            if meta.get("pii", "").upper() != pii or not pii:
                continue
            path = "/".join((meta["path"].strip("/"), pii)) + meta.get("pdfExtension", "")
            query = urlencode(meta.get("queryParams", {}))
            urls.append(urljoin(article_url, "/" + path) + ("?" + query if query else ""))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    if parser.meta.get("citation_pdf_url"):
        urls.append(urljoin(article_url, parser.meta["citation_pdf_url"]))
    urls.extend(parser.links)
    urls = [u for u in dict.fromkeys(urls) if matches_article(u, {**paper, "article_url": article_url})]
    return urls[:1]  # One main-document route; references are not fallback papers.
