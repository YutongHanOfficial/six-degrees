"""Tiny SPARQL client for bulk CSV exports.

Defaults to QLever's public Wikidata endpoint, which streams multi-million-row results
quickly. Each request must finish within the endpoint's time limit (30 s on the public
instance), so large results are fetched in ORDER BY ... LIMIT/OFFSET pages; a page that
times out is retried at half the size. Results are stored gzip-compressed.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = os.environ.get("SIXDEG_SPARQL_ENDPOINT", "https://qlever.dev/api/wikidata")
USER_AGENT = "sixdeg/1.0 (six-degrees people graph builder; personal project)"
# QLever appends this to a streamed result that failed part-way (HTTP status is already 200).
_STREAM_ERROR = b"!!!!>># An error has occurred"

PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""


class QueryError(RuntimeError):
    pass


class QueryTimeout(QueryError):
    pass


class RateLimited(OSError):
    """HTTP 429/503: the endpoint wants fewer requests for a while (retry_after: seconds it asked for)."""

    def __init__(self, msg: str, retry_after: int = 0):
        super().__init__(msg)
        self.retry_after = retry_after


def _request(sparql: str, dest: str, timeout: int) -> int:
    body = urllib.parse.urlencode({"query": PREFIXES + sparql}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Accept": "text/csv",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                shutil.copyfileobj(resp, out, 1 << 20)
            else:
                with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=5) as gz:
                    shutil.copyfileobj(resp, gz, 1 << 20)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
        try:
            msg = json.loads(raw).get("exception", "")
        except ValueError:
            msg = raw[:300].decode("utf-8", "replace")
        if "timed out" in msg.lower():
            raise QueryTimeout(msg) from None
        if e.code == 400:
            raise QueryError(f"query rejected by endpoint: {msg}") from None
        if e.code in (429, 503):
            after = e.headers.get("Retry-After", "")
            raise RateLimited(f"HTTP {e.code}: {msg[:200]}", int(after) if after.isdigit() else 0) from None
        raise OSError(f"HTTP {e.code}: {msg[:200]}") from None
    return _count_rows(dest)


def _count_rows(path: str) -> int:
    """Read the file back: catches truncated gzip streams, error pages and mid-stream failures."""
    n = 0
    prev = tail = b""
    with gzip.open(path, "rb") as f:
        header = f.readline()
        if not header or header.lstrip()[:1] in (b"<", b"{"):
            raise ValueError("unexpected response: %r" % header[:200])
        for line in f:
            n += 1
            prev, tail = tail, line
    if _STREAM_ERROR in prev or _STREAM_ERROR in tail:
        raise QueryTimeout("result stream aborted: " + tail[:200].decode("utf-8", "replace"))
    return n


def run_query(sparql: str, dest: str, retries: int = 5, timeout: int = 600, log=print) -> int:
    """Run one request (with retries on network errors). Returns the row count.

    "Too many requests" gets longer pauses (30 s doubling to 4 min, or what the endpoint asks for),
    since the monthly refresh on GitHub's machines runs into the endpoint's rate limit.
    """
    tmp = dest + ".part"
    last = ""
    for attempt in range(1, retries + 1):
        try:
            rows = _request(sparql, tmp, timeout)
            os.replace(tmp, dest)
            return rows
        except QueryTimeout:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        except QueryError:
            raise
        except RateLimited as e:
            last, wait = repr(e), min(300, max(e.retry_after, 30 * 2 ** (attempt - 1)))
        except (urllib.error.URLError, OSError, EOFError, ValueError) as e:
            last, wait = repr(e), min(120, 5 * 2 ** (attempt - 1))
        if attempt < retries:
            log(f"    retry {attempt}/{retries - 1} in {wait}s: {last[:160]}")
            time.sleep(wait)
    if os.path.exists(tmp):
        os.remove(tmp)
    raise QueryError(f"giving up after {retries} attempts: {last[:400]}")


def run_paged(sparql: str, dest: str, page: int, log=print, min_page: int = 50_000) -> int:
    """Fetch `sparql` in LIMIT/OFFSET pages and concatenate them into `dest`.

    No ORDER BY: sorting would make the engine materialise the whole result for every page.
    QLever's natural order for these subject-keyed scans is deterministic, and the loader
    de-duplicates anyway.
    """
    tmp = dest + ".page"
    total, offset = 0, 0
    with gzip.open(dest + ".part", "wb", compresslevel=5) as out:
        first = True
        while True:
            q = f"{sparql}\nLIMIT {page} OFFSET {offset}"
            try:
                t0 = time.time()
                rows = run_query(q, tmp, log=log)
            except QueryTimeout as e:
                if page // 2 < min_page:
                    raise QueryError(f"times out even with {page:,}-row pages: {e}") from None
                page //= 2
                log(f"    timeout at offset {offset:,}; retrying with {page:,}-row pages")
                continue
            with gzip.open(tmp, "rb") as f:
                header = f.readline()
                if first:
                    out.write(header)
                    first = False
                shutil.copyfileobj(f, out, 1 << 20)
            os.remove(tmp)
            total += rows
            offset += rows
            if rows < page:
                break
            log(f"    {total:,} rows so far ({time.time() - t0:.0f}s/page)")
    os.replace(dest + ".part", dest)
    return total
