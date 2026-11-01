"""Static website export: the whole database as plain files that any static host (GitHub Pages,
Cloudflare Pages, Netlify...) can serve, with the search running in the visitor's browser.

  site/index.html, app.js ...   the web app from sixdeg/web/, switched to its static engine
  site/data/meta.json           counts, code tables, and where everything is
  site/data/graph-*.bin         the search graph (~30 MB): downloaded once, cached by the browser
  site/data/<pack>-<n>.bin      everything else, as small gzip'd JSON shards packed into a few big
  site/data/<pack>.json         files; the page reads single shards with HTTP range requests

Packs, each sharded by its key:
  cards      person id -> [qid, name, description, birth, death, gender, sitelinks, image, enwiki,
             n_relations, n_contexts]
  links      person id -> 0 | [relations, memberships]: everything needed to explain a link or
             list someone's connections
  contexts   context id -> [kind, qid, name, year, period, sitelinks, size, first five members]
  search     name index: [token, person ids, the other words of that name or alias] groups
  searchtop  best matches for name-index ranges too big to scan ("jo", "john", "smith")
  qid        Wikidata number -> person id

Context ids are renumbered: grouped by category, best known first, a team's seasons together.
The graph renumbers people again, densely over the 4 million who have any link at all.
"""
from __future__ import annotations

import array
import csv
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import time
import unicodedata
import urllib.parse

import numpy as np

from . import CATEGORIES, DEFAULT_OFF
from .store import RELATION_PRIORITY, _YEARS_SUFFIX

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
WEB_FILES = ("index.html", "style.css", "app.js", "engine.js", "worker.js")
KINDS = ["work", "authored", "group", "team", "election", "award", "event", "document", "org", "spaceflight"]
FOLDED = ("team", "award")     # a team's seasons, an award's years: shown as one entry
PART_BYTES = 20_000_000        # under GitHub's 50 MB warning and Cloudflare Pages' 25 MiB limit
GRAPH_PARTS = 4                # membership lists are split so the browser can fetch them in parallel
TOP_N = 10                     # best matches kept for very common names and prefixes
BIG_RANGE = 1500               # name-index ranges with more entries than this get them
CAT_KEYS = [k for k, _ in CATEGORIES]
REL_CODE = {t: i for i, t in enumerate(RELATION_PRIORITY)}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def varint(values) -> bytes:
    """Unsigned LEB128 encoding of every value, concatenated."""
    a = np.asarray(values, np.uint64)
    if a.size == 0:
        return b""
    n = np.ones(a.size, np.int64)
    v = a >> np.uint64(7)
    while v.any():
        n += v > 0
        v >>= np.uint64(7)
    out = np.empty(int(n.sum()), np.uint8)
    start = np.cumsum(n) - n
    for j in range(int(n.max())):
        m = n > j
        byte = ((a[m] >> np.uint64(7 * j)) & np.uint64(0x7F)).astype(np.uint8)
        byte |= np.where(n[m] - 1 > j, 0x80, 0).astype(np.uint8)
        out[start[m] + j] = byte
    return out.tobytes()


def _gz(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


def _json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


# ---------------------------------------------------------------- name tokens

def _char_class(keep) -> str:
    """A regex character class of every code point whose Unicode category passes `keep`."""
    out, start = [], None
    for cp in range(0x110001):
        ok = cp < 0x110000 and keep(unicodedata.category(chr(cp)))
        if ok and start is None:
            start = cp
        elif not ok and start is not None:
            out.append(re.escape(chr(start)) + ("-" + re.escape(chr(cp - 1)) if cp - 1 > start else ""))
            start = None
    return "[" + "".join(out) + "]"


# Same rules as tokens() in engine.js: NFKD, drop non-spacing marks (accents), lower case, then
# runs of letters, digits and spacing marks.
_MN = {cp: None for cp in range(0x110000) if unicodedata.category(chr(cp)) == "Mn"}
_WORD = re.compile(_char_class(lambda c: c[0] in "LN" or c in ("Mc", "Me")) + "+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list:
    if text.isascii():
        return _ASCII_WORD.findall(text.lower())
    return _WORD.findall(unicodedata.normalize("NFKD", text).translate(_MN).lower())


# ---------------------------------------------------------------- packs

class Pack:
    """Shards of gzip'd JSON appended to part files. <name>.json lists each shard's first key and
    length; offsets follow from the lengths, restarting at 0 in every part."""

    def __init__(self, out_dir: str, name: str):
        self.out_dir, self.name = out_dir, name
        self.parts, self.part_start, self.first, self.lens = [], [], [], []
        self.f = None
        self.raw = 0

    def write(self, first_key, payload: bytes):
        blob = _gz(payload)
        if self.f is None or self.f.tell() + len(blob) > PART_BYTES:
            if self.f:
                self.f.close()
            self.parts.append(f"{self.name}-{len(self.parts)}.bin")
            self.part_start.append(len(self.lens))
            self.f = open(os.path.join(self.out_dir, self.parts[-1]), "wb")
        self.f.write(blob)
        self.first.append(first_key)
        self.lens.append(len(blob))
        self.raw += len(payload)

    def close(self) -> dict:
        if self.f:
            self.f.close()
        numeric = bool(self.first) and isinstance(self.first[0], int)
        first = list(np.diff(self.first, prepend=0).tolist()) if numeric else self.first
        with open(os.path.join(self.out_dir, self.name + ".json"), "w") as f:
            json.dump({"parts": self.parts, "part_start": self.part_start, "delta": numeric,
                       "first": first, "len": self.lens}, f, separators=(",", ":"), ensure_ascii=False)
        packed = sum(self.lens)
        log(f"  {self.name}: {len(self.lens):,} shards in {len(self.parts)} files, "
            f"{packed / 1e6:.1f} MB ({self.raw / 1e6:.0f} MB of JSON)")
        return {"index": self.name + ".json", "shards": len(self.lens), "bytes": packed}


class Sharder:
    """Groups consecutive records into JSON-array shards of about `budget` bytes."""

    def __init__(self, pack: Pack, budget: int):
        self.pack, self.budget = pack, budget
        self.items, self.first, self.size = [], None, 0

    def add(self, key, record):
        b = _json(record)
        if self.items and self.size + len(b) > self.budget:
            self.flush()
        if not self.items:
            self.first = key
        self.items.append(b)
        self.size += len(b) + 1

    def flush(self):
        if self.items:
            self.pack.write(self.first, b"[" + b",".join(self.items) + b"]")
            self.items, self.size = [], 0

    def close(self) -> dict:
        self.flush()
        return self.pack.close()


# ---------------------------------------------------------------- contexts

class Contexts:
    """The context table in memory, plus the web numbering."""

    def __init__(self, con: sqlite3.Connection):
        C = con.execute("SELECT COUNT(*) FROM context").fetchone()[0]
        self.C = C
        self.qnum = np.empty(C, np.int64)
        self.kind = np.empty(C, np.int64)
        self.cat = np.empty(C, np.int64)
        self.year = np.zeros(C, np.int64)
        self.has_year = np.zeros(C, bool)
        self.period = np.zeros(C, np.int64)
        self.has_period = np.zeros(C, bool)
        self.sl = np.empty(C, np.int64)
        self.size = np.empty(C, np.int64)
        self.names = [None] * C
        kind_code = {k: i for i, k in enumerate(KINDS)}
        cat_code = {k: i for i, k in enumerate(CAT_KEYS)}
        for cid, qid, kind, cat, name, year, period, sl, size in con.execute(
                "SELECT id, qid, kind, category, name, year, period, sitelinks, size FROM context ORDER BY id"):
            self.qnum[cid] = int(qid[1:])
            self.kind[cid] = kind_code[kind]
            self.cat[cid] = cat_code[cat]
            if year is not None:
                self.year[cid], self.has_year[cid] = year, True
            if period is not None:
                self.period[cid], self.has_period[cid] = period, True
            self.sl[cid], self.size[cid] = sl, size
            self.names[cid] = name

        # Web order: by category, best known first, then latest first -- the order a person's
        # connections are listed in -- keeping a team's seasons (an award's years) together.
        self.folded = np.isin(self.kind, [kind_code[k] for k in FOLDED])
        y = np.where(self.has_year, self.year, -(10 ** 9))
        key = np.where(self.folded, self.kind * (1 << 40) + self.qnum, (1 << 50) + np.arange(C))
        o = np.argsort(key, kind="stable")
        new_run = np.r_[True, key[o][1:] != key[o][:-1]]
        group_latest = np.maximum.reduceat(y[o], np.flatnonzero(new_run))
        latest = np.empty(C, np.int64)
        latest[o] = group_latest[np.cumsum(new_run) - 1]
        self.order = np.lexsort((np.arange(C), -y, key, -latest, -self.sl, self.cat))  # web id -> db id
        self.web = np.empty(C, np.int64)                                               # db id -> web id
        self.web[self.order] = np.arange(C)
        self.cat_start = np.searchsorted(self.cat[self.order], np.arange(len(CATEGORIES) + 1)).tolist()


# ---------------------------------------------------------------- graph

def export_graph(con, graph_dir: str, ctx: Contexts, out_dir: str) -> dict:
    """Varint streams the browser turns back into the CSR arrays graph.py uses.

    graph-persons.bin   linked people as gaps between their ids, then their sitelinks
    graph-contexts.bin  per web context: sitelinks, then size
    graph-members-N.bin per web context: its members' graph ids, ascending, as deltas
    graph-direct.bin    direct links (a < b, both graph ids), sorted: a gaps, b deltas, categories
    """
    load = lambda n: np.load(os.path.join(graph_dir, n + ".npy"))
    d_ptr, d_nbr, d_cat = load("d_ptr"), load("d_nbr"), load("d_cat")
    pc_ptr, cp_ptr, cp_per = load("pc_ptr"), load("cp_ptr"), load("cp_per")
    P = d_ptr.size - 1
    linked = (np.diff(d_ptr) + np.diff(pc_ptr)) > 0
    gid = (np.cumsum(linked) - 1).astype(np.int64)
    linked_ids = np.flatnonzero(linked)
    L = int(linked_ids.size)
    files = []

    def put(name, data: bytes):
        blob = _gz(data)
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(blob)
        files.append({"name": name, "bytes": len(blob)})

    sl = np.fromiter((r[0] for r in con.execute("SELECT sitelinks FROM person ORDER BY id")), np.int64, P)
    put("graph-persons.bin", varint(np.diff(linked_ids, prepend=-1) - 1) + varint(sl[linked_ids]))

    sizes = np.diff(cp_ptr)[ctx.order]
    put("graph-contexts.bin", varint(ctx.sl[ctx.order]) + varint(sizes))

    # members of each context in web order, as graph ids (gid keeps id order, so rows stay sorted)
    starts = cp_ptr[:-1][ctx.order]
    ends = np.cumsum(sizes)
    pos = np.arange(int(ends[-1]), dtype=np.int64) + np.repeat(starts - (ends - sizes), sizes)
    mem = gid[cp_per[pos]]
    first = np.zeros(mem.size, bool)
    first[(ends - sizes)[sizes > 0]] = True
    delta = np.where(first, mem, mem - np.r_[0, mem[:-1]])
    bounds = [0]                                   # split at context boundaries, ~equal sizes
    for k in range(1, GRAPH_PARTS):
        bounds.append(int(np.searchsorted(ends, mem.size * k // GRAPH_PARTS)) + 1)
    bounds.append(ctx.C)
    member_parts = []
    for k in range(GRAPH_PARTS):
        a, b = bounds[k], bounds[k + 1]
        lo, hi = int(ends[a - 1]) if a else 0, int(ends[b - 1]) if b else 0
        put(f"graph-members-{k}.bin", varint(delta[lo:hi]))
        member_parts.append({"name": files[-1]["name"], "contexts": [a, b], "count": hi - lo})

    src = np.repeat(np.arange(P), np.diff(d_ptr))
    keep = src < d_nbr
    a, b, c = gid[src[keep]], gid[d_nbr[keep]], d_cat[keep].astype(np.int64)
    o = np.lexsort((c, b, a))
    a, b, c = a[o], b[o], c[o]
    same_a = np.r_[False, a[1:] == a[:-1]]
    put("graph-direct.bin", varint(np.diff(a, prepend=0)) +
        varint(np.where(same_a, b - np.r_[0, b[:-1]], b - a)) + c.astype(np.uint8).tobytes())

    log(f"  graph: {L:,} linked people, {ctx.C:,} contexts, {mem.size:,} memberships, "
        f"{a.size:,} direct links, {sum(f['bytes'] for f in files) / 1e6:.1f} MB")
    return {"persons": P, "linked": L, "contexts": ctx.C, "memberships": int(mem.size),
            "direct": int(a.size), "cat_start": ctx.cat_start, "files": files, "members": member_parts}


# ---------------------------------------------------------------- people

def export_cards(con, out_dir: str):
    """Person cards in id order; also returns each person's qid number (for the qid pack)."""
    P = con.execute("SELECT COUNT(*) FROM person").fetchone()[0]
    qnums = np.empty(P, np.int64)
    linked = np.zeros(P, bool)
    sharder = Sharder(Pack(out_dir, "cards"), 24_000)
    gender = {"m": "m", "f": "f"}
    for (pid, qid, name, desc, birth, death, g, sl, image, enwiki, n_rel, n_ctx) in con.execute(
            "SELECT id, qid, name, description, birth_year, death_year, gender, sitelinks, image, enwiki, "
            "n_relations, n_contexts FROM person ORDER BY id"):
        q = qnums[pid] = int(qid[1:])
        linked[pid] = n_rel + n_ctx > 0
        if desc and (birth is not None or death is not None):
            desc = _YEARS_SUFFIX.sub("", desc) or desc
        wiki = 0 if not enwiki else 1 if enwiki == name else enwiki
        sharder.add(pid, [q, name, desc or "", birth, death, gender.get(g, ""), sl, image or "", wiki, n_rel, n_ctx])
    return sharder.close(), qnums, linked


def export_links(con, ctx: Contexts, out_dir: str):
    """Each person's relations and memberships, in the order the person panel lists them."""
    P = con.execute("SELECT COUNT(*) FROM person").fetchone()[0]
    roles, role_code = [], {}
    mp, mc, mr = array.array("q"), array.array("q"), array.array("q")
    for p, c, r in con.execute("SELECT person_id, context_id, role FROM membership"):
        code = role_code.get(r)
        if code is None:
            code = role_code[r] = len(roles)
            roles.append(r or "")
        mp.append(p)
        mc.append(c)
        mr.append(code)
    mp, mc, mr = np.frombuffer(mp, np.int64), np.frombuffer(mc, np.int64), np.frombuffer(mr, np.int64)
    wc = ctx.web[mc]
    o = np.lexsort((wc, mp))
    mp, wc, mr, mf = mp[o], wc[o], mr[o], np.where(ctx.folded[mc[o]], ctx.qnum[mc[o]], 0)
    m_ptr = np.searchsorted(mp, np.arange(P + 1))

    ra, rb, rt, rc, ry, rd = [array.array("q") for _ in range(5)] + [[]]
    cat_code = {k: i for i, k in enumerate(CAT_KEYS)}
    for a, b, t, c, d, y in con.execute("SELECT a_id, b_id, type, category, detail, year FROM relation"):
        ra.append(a)
        rb.append(b)
        rt.append(REL_CODE[t])
        rc.append(cat_code[c])
        ry.append(y if y is not None else -(10 ** 9))
        rd.append(d)
    ra, rb, rt = np.frombuffer(ra, np.int64), np.frombuffer(rb, np.int64), np.frombuffer(rt, np.int64)
    R = ra.size
    end = np.r_[ra, rb]
    other = np.r_[rb, ra]
    idx = np.r_[np.arange(R), np.arange(R)]
    is_a = np.r_[np.ones(R, np.int64), np.zeros(R, np.int64)]
    o = np.lexsort((other, rt[idx], end))
    end, other, idx, is_a = end[o], other[o], idx[o], is_a[o]
    r_ptr = np.searchsorted(end, np.arange(P + 1))
    rc, ry = np.frombuffer(rc, np.int64), np.frombuffer(ry, np.int64)

    sharder = Sharder(Pack(out_dir, "links"), 24_000)
    block = 8192
    for lo in range(0, P, block):
        hi = min(P, lo + block)
        r0, r1, m0, m1 = int(r_ptr[lo]), int(r_ptr[hi]), int(m_ptr[lo]), int(m_ptr[hi])
        R_other, R_idx, R_isa = other[r0:r1].tolist(), idx[r0:r1].tolist(), is_a[r0:r1].tolist()
        M_w, M_r, M_f = wc[m0:m1].tolist(), mr[m0:m1].tolist(), mf[m0:m1].tolist()
        rp, mpp = (r_ptr[lo:hi + 1] - r0).tolist(), (m_ptr[lo:hi + 1] - m0).tolist()
        for i in range(hi - lo):
            rels = []
            for j in range(rp[i], rp[i + 1]):
                k = R_idx[j]
                y = int(ry[k])
                rels.append([R_other[j], int(rt[k]), R_isa[j], int(rc[k]), y if y > -(10 ** 9) else None, rd[k]])
            mems, prev = [], 0
            for j in range(mpp[i], mpp[i + 1]):
                w = M_w[j]
                mems.append([w - prev, M_r[j], M_f[j]] if M_f[j] else [w - prev, M_r[j]])
                prev = w
            sharder.add(lo + i, [rels, mems] if rels or mems else 0)
    return sharder.close(), roles


def export_contexts(ctx: Contexts, graph_dir: str, out_dir: str) -> dict:
    cp_ptr, cp_per = np.load(os.path.join(graph_dir, "cp_ptr.npy")), np.load(os.path.join(graph_dir, "cp_per.npy"))
    sharder = Sharder(Pack(out_dir, "contexts"), 12_000)
    order = ctx.order.tolist()
    for w, c in enumerate(order):
        s = int(cp_ptr[c])
        sharder.add(w, [int(ctx.kind[c]), int(ctx.qnum[c]), ctx.names[c],
                        int(ctx.year[c]) if ctx.has_year[c] else None,
                        int(ctx.period[c]) if ctx.has_period[c] else None,
                        int(ctx.sl[c]), int(ctx.size[c]), cp_per[s:min(s + 5, int(cp_ptr[c + 1]))].tolist()])
    return sharder.close()


def export_qids(qnums: np.ndarray, out_dir: str) -> dict:
    o = np.argsort(qnums, kind="stable")
    q, ids = qnums[o], o
    pack = Pack(out_dir, "qid")
    for lo in range(0, q.size, 4096):
        qs = q[lo:lo + 4096]
        pack.write(int(qs[0]), _json([np.diff(qs, prepend=qs[0]).tolist(), ids[lo:lo + 4096].tolist()]))
    return pack.close()


# ---------------------------------------------------------------- name index

def export_search(con, raw_dir: str, qnums: np.ndarray, tmp_path: str, out_dir: str) -> dict:
    """Entries (token, person, the other tokens of that name/alias), sorted by token then person.
    A query is answered from the entries of one of its words: the others are checked against the
    words stored alongside. Persons are ranked by id, i.e. best known first."""
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    tmp = sqlite3.connect(tmp_path, isolation_level=None)
    tmp.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-400000;"
                      "CREATE TABLE e(t TEXT NOT NULL, id INTEGER NOT NULL, o TEXT NOT NULL);")

    aliases = {}
    path = os.path.join(raw_dir, "people_alias.csv.gz")
    if os.path.exists(path):
        order = np.argsort(qnums)
        sq = qnums[order]
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            rows = csv.reader(f)
            next(rows, None)
            for row in rows:
                if len(row) < 2 or not row[0].startswith("Q"):
                    continue
                q = int(row[0][1:])
                i = int(np.searchsorted(sq, q))
                if i < sq.size and sq[i] == q:
                    aliases.setdefault(int(order[i]), []).append(row[1])
    log(f"  name index: {sum(map(len, aliases.values())):,} aliases for {len(aliases):,} people")

    batch = []

    def emit(pid, text, seen):
        toks = tokens(text)
        key = " ".join(toks)
        if not toks or key in seen:
            return
        seen.add(key)
        uniq = list(dict.fromkeys(toks))
        for t in uniq:
            batch.append((t, pid, " ".join(x for x in uniq if x != t)))

    tmp.execute("BEGIN")
    for pid, name in con.execute("SELECT id, name FROM person ORDER BY id"):
        seen = set()
        emit(pid, name, seen)
        for al in aliases.get(pid, ()):
            emit(pid, al, seen)
        if len(batch) > 200_000:
            tmp.executemany("INSERT INTO e VALUES (?,?,?)", batch)
            batch.clear()
    tmp.executemany("INSERT INTO e VALUES (?,?,?)", batch)
    tmp.execute("COMMIT")
    del aliases

    # Which exact tokens and prefixes are too big to scan in the browser? Tokens arrive sorted, so
    # every prefix is one contiguous run: keep a running count per prefix length.
    big_exact, big_prefix = set(), set()
    run = [None] * 17                       # run[k] = [prefix of length k, entries so far]
    for t, n in tmp.execute("SELECT t, COUNT(*) FROM e GROUP BY t ORDER BY t"):
        if n > BIG_RANGE:
            big_exact.add(t)
        for k in range(2, 17):
            p = t[:k] if len(t) >= k else None
            if run[k] and run[k][0] != p:
                if run[k][1] > BIG_RANGE:
                    big_prefix.add(run[k][0])
                run[k] = None
            if p is not None:
                if run[k]:
                    run[k][1] += n
                else:
                    run[k] = [p, n]
    big_prefix.update(r[0] for r in run if r and r[1] > BIG_RANGE)
    log(f"  name index: {len(big_exact):,} common words and {len(big_prefix):,} prefixes get precomputed matches")
    tops = {}

    def take_top(key, ids):
        cur = tops.get(key)
        if cur is None:
            tops[key] = ids[:TOP_N]
        else:
            tops[key] = sorted(set(cur) | set(ids[:TOP_N]))[:TOP_N]

    pack = Pack(out_dir, "search")
    shard, shard_size, shard_first = [], 0, None
    budget = 64_000

    def flush():
        nonlocal shard, shard_size
        if shard:
            pack.write(shard_first, _json(shard))
            shard, shard_size = [], 0

    def add_group(t, ids, others):
        nonlocal shard_size, shard_first
        distinct = list(dict.fromkeys(ids))
        if t in big_exact:
            take_top("e:" + t, distinct)
        for k in range(2, min(len(t), 16) + 1):
            if t[:k] in big_prefix:
                take_top("p:" + t[:k], distinct)
        i = 0
        while i < len(ids):           # a big group is split across shards
            if not shard:
                shard_first = t
            room = max(1, (budget - shard_size) // 12)
            chunk_ids, chunk_o = ids[i:i + room], others[i:i + room]
            entry = [t, np.diff(chunk_ids, prepend=0).tolist(), chunk_o]
            shard.append(entry)
            shard_size += len(t) + sum(len(o) + 9 for o in chunk_o)
            i += len(chunk_ids)
            if shard_size >= budget:
                flush()

    cur_t, ids, others = None, [], []
    for t, pid, o in tmp.execute("SELECT t, id, o FROM e ORDER BY t, id"):
        if t != cur_t:
            if cur_t is not None:
                add_group(cur_t, ids, others)
            cur_t, ids, others = t, [], []
        ids.append(pid)
        others.append(o)
    if cur_t is not None:
        add_group(cur_t, ids, others)
    flush()
    info = pack.close()
    tmp.close()
    os.remove(tmp_path)

    top_pack = Pack(out_dir, "searchtop")
    items = sorted(tops.items())
    for lo in range(0, len(items), 400):
        chunk = items[lo:lo + 400]
        top_pack.write(chunk[0][0], _json([[k, v] for k, v in chunk]))
    return {"search": info, "searchtop": top_pack.close(), "top_n": TOP_N}


# ---------------------------------------------------------------- entry point

def export_site(data_dir: str, out_dir: str, web: bool = True) -> None:
    t0 = time.time()
    db_path = os.path.join(data_dir, "people.db")
    graph_dir = os.path.join(data_dir, "graph")
    data_out = os.path.join(out_dir, "data")
    if os.path.isdir(data_out):
        shutil.rmtree(data_out)
    os.makedirs(data_out)
    con = sqlite3.connect("file:" + urllib.parse.quote(db_path) + "?mode=ro", uri=True)
    meta = {k: v for k, v in con.execute("SELECT key, value FROM meta")}

    log("contexts")
    ctx = Contexts(con)
    log("graph")
    graph = export_graph(con, graph_dir, ctx, data_out)
    log("cards")
    cards, qnums, linked = export_cards(con, data_out)
    log("links")
    links, roles = export_links(con, ctx, data_out)
    log("contexts pack")
    contexts = export_contexts(ctx, graph_dir, data_out)
    log("qid index")
    qid = export_qids(qnums, data_out)
    log("name index")
    search = export_search(con, os.path.join(data_dir, "raw"), qnums,
                           os.path.join(data_dir, "site-index.tmp.db"), data_out)
    con.close()

    version = re.sub(r"\D", "", meta.get("built_at", "")) or str(int(time.time()))
    site_meta = {
        "version": version,
        "stats": {
            "people": int(meta["count_person"]), "contexts": int(meta["count_context"]),
            "memberships": int(meta["count_membership"]), "relations": int(meta["count_relation"]),
            "built_at": meta.get("built_at"), "source": meta.get("source"),
        },
        "categories": [{"key": k, "label": lbl, "default": k not in DEFAULT_OFF} for k, lbl in CATEGORIES],
        "kinds": KINDS,
        "relation_types": RELATION_PRIORITY,
        "roles": roles,
        "graph": graph,
        "packs": {"cards": cards, "links": links, "contexts": contexts, "qid": qid,
                  "search": search["search"], "searchtop": search["searchtop"]},
        "top_n": search["top_n"],
        # "Surprise me" picks two of the 3,000 best-known people who have at least one link
        "random_pool": np.flatnonzero(linked[:3000]).tolist(),
    }
    with open(os.path.join(data_out, "meta.json"), "w") as f:
        json.dump(site_meta, f, separators=(",", ":"), ensure_ascii=False)
    if web:
        copy_web(out_dir, version)
    total = sum(os.path.getsize(os.path.join(data_out, n)) for n in os.listdir(data_out))
    log(f"site written to {out_dir}: {len(os.listdir(data_out))} data files, {total / 1e6:.0f} MB "
        f"({(time.time() - t0) / 60:.1f} min)")


def copy_web(out_dir: str, version: str) -> None:
    """The web app, with engine.js switched on. Asset URLs carry the data version so browsers
    pick up a new build instead of a cached old one."""
    for name in WEB_FILES:
        with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
            text = f.read()
        if name == "index.html":
            text = text.replace('<script src="app.js"></script>',
                                f'<script>window.SIXDEG_STATIC = {{ data: "data/", version: "{version}" }};</script>\n'
                                f'  <script src="engine.js?v={version}"></script>\n'
                                f'  <script src="app.js?v={version}"></script>')
            text = text.replace('href="style.css"', f'href="style.css?v={version}"')
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(text)
    # GitHub Pages would otherwise run the files through Jekyll
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
