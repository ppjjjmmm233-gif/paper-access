import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .config import PaperError
from .metadata import norm
from .routes import publisher_key


def validate_pdf(data, paper):
    if not data.lstrip().startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
        raise PaperError("invalid_pdf", "Response is not a complete PDF")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise PaperError("dependency_error", "Install requirements.txt to validate PDFs") from None
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted or not len(reader.pages):
            raise PaperError("invalid_pdf", "PDF is encrypted or has no pages")
        texts = [(page.extract_text() or "") for page in reader.pages]
        first = "\n".join(texts[:2])
        doi, title = paper.get("doi", ""), norm(paper.get("title", ""))
        doi_found = bool(doi and re.search(r"(?<!\w)" + re.escape(doi) + r"(?![\w/]|[.();:+-]\w)", first, re.I))
        title_found = bool(len(title) >= 12 and title in norm(first))
        if not (doi_found or title_found):
            raise PaperError("identity_unverified", "PDF identity did not match requested DOI/title")
        first_lines = [line.strip() for line in first.splitlines() if line.strip()][:8]
        if any(re.fullmatch(r"(?:supporting|supplementary|supplemental)\s+(?:information|materials?|data)", line, re.I)
               for line in first_lines):
            raise PaperError("invalid_pdf", "Candidate is supplementary material, not the article")
        return {"pages": len(reader.pages), "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "identity_evidence": "doi" if doi_found else "title",
                "validation": "all_pages_parsed_and_text_extracted", "text_characters": sum(map(len, texts))}
    except PaperError:
        raise
    except Exception:
        raise PaperError("invalid_pdf", "PDF parser could not read every page") from None


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.part")
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3", timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS papers (key TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS cooldowns (host TEXT PRIMARY KEY, until REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts (host TEXT PRIMARY KEY, at REAL NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def key(self, paper):
        return paper.get("doi") or hashlib.sha256(norm(paper.get("title", "")).encode()).hexdigest()

    def cached(self, paper, output):
        row = self.db.execute("SELECT receipt FROM papers WHERE key=?", (self.key(paper),)).fetchone()
        if not row:
            return None
        receipt = json.loads(row[0])
        path = Path(receipt["file"])
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
            evidence = validate_pdf(data, paper)
            if evidence["sha256"] != receipt.get("sha256"):
                return None
        except PaperError:
            return None
        target = Path(output) / path.name
        if target.resolve() != path.resolve():
            atomic_write(target, data)
        result = {**receipt, **evidence, "file": str(target.resolve()), "status": "cached"}
        atomic_write(target.with_suffix(".json"), json.dumps(result, ensure_ascii=False, indent=2).encode())
        return result

    def save(self, data, paper, output, source, version="unknown"):
        evidence = validate_pdf(data, paper)
        key = self.key(paper)
        stem = re.sub(r"[^\w.-]+", "_", key)[:100] + "_" + hashlib.sha256(key.encode()).hexdigest()[:10]
        path = Path(output).resolve() / (stem + ".pdf")
        receipt = {"status": "downloaded", "doi": paper.get("doi"), "title": paper.get("title", ""),
                   "file": str(path), "source": source, "version": version, **evidence}
        atomic_write(path, data)
        atomic_write(path.with_suffix(".json"), json.dumps(receipt, ensure_ascii=False, indent=2).encode())
        self.db.execute("INSERT OR REPLACE INTO papers VALUES (?, ?)", (key, json.dumps(receipt)))
        self.db.commit()
        return receipt

    def cooldown(self, host, seconds=None):
        original = host
        host = publisher_key(host)
        if seconds is not None:
            self.db.execute("INSERT OR REPLACE INTO cooldowns VALUES (?, ?)", (host, time.time() + seconds))
            self.db.commit()
        aliases = [host, original]
        if host == "sciencedirect.com":
            aliases += ["www.sciencedirect.com", "pdf.sciencedirectassets.com"]
        rows = [self.db.execute("SELECT until FROM cooldowns WHERE host=?", (key,)).fetchone() for key in aliases]
        until = max((row[0] for row in rows if row), default=0)
        return max(0, int(until - time.time()) + 1) if until else 0

    def space_request(self, host, delay):
        row = self.db.execute("SELECT at FROM attempts WHERE host=?", (host,)).fetchone()
        if row and delay:
            time.sleep(max(0, row[0] + delay - time.time()))
        self.db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?)", (host, time.time()))
        self.db.commit()


class ProfileLock:
    """OS-managed lock; process crashes release it without stale file deletion."""
    def __init__(self, path):
        self.path, self.handle = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise PaperError("profile_busy", "Another command owns this browser profile") from None
        return self

    def __exit__(self, *_):
        self.handle.close()
