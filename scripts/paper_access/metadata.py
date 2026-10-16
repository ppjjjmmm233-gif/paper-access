import html
import re
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin

from .config import PaperError
from .net import get_json, http_get


def normalize_doi(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I)
    if not re.fullmatch(r"10\.\d{4,9}/[^\s<>]+", value, re.I):
        raise PaperError("invalid_identifier", "Expected a DOI such as 10.1234/example")
    return value.lower()


def norm(text):
    return re.sub(r"[^\w]+", "", html.unescape(text).casefold())


class CitationHTML(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.meta, self.links = base, {}, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            self.meta.setdefault(key, attrs.get("content", ""))
        if tag == "a" and attrs.get("href"):
            link = urljoin(self.base, attrs["href"])
            if re.search(r"(?:\.pdf(?:[?#]|$)|/pdf(?:direct|ft)?(?:[/?#]|$))", link, re.I):
                self.links.append(link)


def parse_article(data, url):
    parser = CitationHTML(url)
    parser.feed(data.decode("utf-8", errors="replace"))
    meta = parser.meta
    paper = {"doi": meta.get("citation_doi") or meta.get("dc.identifier", ""),
             "title": meta.get("citation_title") or meta.get("dc.title", ""), "article_url": url}
    from .routes import main_pdf_urls
    paper["pdf_urls"] = main_pdf_urls(data.decode("utf-8", errors="replace"), url, paper)
    return paper


def from_crossref(item):
    return {"doi": item.get("DOI", ""), "title": (item.get("title") or [""])[0],
            "article_url": item.get("URL", ""), "publisher": item.get("publisher", ""),
            "year": ((item.get("issued", {}).get("date-parts") or [[None]])[0] or [None])[0]}


def resolve(cfg, doi=None, title=None, url=None):
    crossref = cfg.get("crossref_url", "https://api.crossref.org/works").rstrip("/")
    if title and not doi and not url:
        payload = get_json(crossref + "?" + urlencode({"query.bibliographic": title, "rows": 5}), cfg)
        return {"status": "candidates", "query": title,
                "candidates": [from_crossref(i) for i in payload.get("message", {}).get("items", [])]}
    if url:
        try:
            data, final, _ = http_get(url, cfg)
            article = parse_article(data, final)
        except PaperError:
            if not doi:
                raise
            # An explicit DOI supplies identity even if a publisher blocks the
            # unauthenticated HTTP metadata request. Continue to browser SSO.
            article = {"doi": doi, "title": title or "", "article_url": url, "pdf_urls": []}
        doi = doi or article.get("doi")
        if not doi and not (title or article.get("title")):
            raise PaperError("identity_unverified", "Article exposes no DOI/title; supply --doi or --title")
        if not doi:
            return {**article, "title": title or article["title"]}
    doi = normalize_doi(doi)
    result = {"doi": doi, "title": title or "", "article_url": url or "https://doi.org/" + quote(doi, safe="/")}
    try:
        item = get_json(crossref + "/" + quote(doi, safe=""), cfg).get("message", {})
        result.update({k: v for k, v in from_crossref(item).items() if v and k not in {"doi", "article_url"}})
    except PaperError:
        pass
    if url:
        result["pdf_urls"] = article["pdf_urls"]
    return result


def oa_locations(paper, cfg):
    urls = [{"url": u, "source": "article_page", "version": "unknown"} for u in paper.get("pdf_urls", [])]
    doi = paper.get("doi")
    if not doi:
        return urls
    base = cfg.get("openalex_url", "https://api.openalex.org/works").rstrip("/")
    try:
        url = base + "/" + quote("https://doi.org/" + doi, safe=":/")
        if cfg.get("openalex_api_key_env"):
            import os
            key = os.environ.get(cfg["openalex_api_key_env"])
            if key:
                url += "?" + urlencode({"api_key": key})
        payload = get_json(url, cfg)
        for location in payload.get("locations", []):
            if location.get("is_oa") and location.get("pdf_url"):
                urls.append({"url": location["pdf_url"], "source": "openalex",
                             "version": location.get("version") or "unknown"})
    except PaperError:
        pass
    return list({x["url"]: x for x in urls}.values())
