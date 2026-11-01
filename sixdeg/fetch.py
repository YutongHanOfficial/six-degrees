"""Download step: run every bulk Wikidata query once and cache the CSVs under data/raw/."""
from __future__ import annotations

import os
import time

from . import sparql, wikidata


def raw_path(raw_dir: str, name: str) -> str:
    return os.path.join(raw_dir, name + ".csv.gz")


def fetch_all(raw_dir: str, refresh: bool = False, only=None, log=print) -> None:
    os.makedirs(raw_dir, exist_ok=True)
    queries = wikidata.bulk_queries()
    if only:
        queries = [q for q in queries if any(q.name.startswith(o) for o in only)]
    failed = []
    t_all = time.time()
    for i, q in enumerate(queries, 1):
        dest = raw_path(raw_dir, q.name)
        if os.path.exists(dest) and not refresh:
            continue
        t0 = time.time()
        log(f"[{i:2d}/{len(queries)}] {q.name} ...")
        try:
            rows = sparql.run_paged(q.sparql, dest, q.page, log=log)
        except sparql.QueryError as e:
            log(f"    FAILED: {e}")
            failed.append(q.name)
            continue
        size = os.path.getsize(dest) / 1e6
        log(f"    {rows:,} rows, {size:.1f} MB in {time.time() - t0:.0f}s")
    log(f"fetch finished in {time.time() - t_all:.0f}s")
    if failed:
        raise SystemExit("queries failed: " + ", ".join(failed) + " (re-run to retry just these)")
