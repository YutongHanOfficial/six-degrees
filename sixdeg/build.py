"""Turn the raw Wikidata CSVs into data/people.db (SQLite) and data/graph/*.npy (search graph).

Pipeline:  raw/*.csv.gz --stage()--> stage.db --assemble()--> people.db --export_graph()--> graph/
stage.db is scratch space and is deleted at the end.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sqlite3
import time

import numpy as np

from . import CATEGORIES, CATEGORY_CODE, sparql, wikidata
from .fetch import raw_path

NOPERIOD = -999999  # period column value for contexts that are not split by season/year

# Largest context that still counts as "these people know each other".
# (authored: counted over authors with a Wikipedia/Wikimedia page, so it also drops most mega-collaborations)
CAPS = {"work": 250, "authored": 25, "group": 60, "team": 80, "election": 15, "award": 20,
        "event": 40, "document": 100, "org": 20, "spaceflight": 20}

RELATION_CATEGORY = {
    "spouse": "family", "partner": "family", "parent": "family", "sibling": "family",
    "relative": "family", "stepparent": "family", "godparent": "family",
    "advisor": "science", "teacher": "science", "influence": "influence",
    "partner_sport": "sports", "significant": "other", "succession": "politics",
}
SYMMETRIC = {"spouse", "partner", "sibling", "partner_sport"}
FAMILY_CATEGORY = {"team": "sports", "election": "politics", "award": "awards",
                   "event": "other", "document": "politics", "org": "business", "spaceflight": "other"}
GENDER = {"Q6581097": "m", "Q6581072": "f", "Q2449503": "m", "Q1052281": "f"}

_now = time.gmtime()
CURRENT_SEASON = _now.tm_year if _now.tm_mon >= 7 else _now.tm_year - 1


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


# ---------------------------------------------------------------- parsing helpers

def rows(raw_dir: str, name: str, *cols: str):
    """Yield tuples of the requested CSV columns ('' when absent)."""
    with gzip.open(raw_path(raw_dir, name), "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        idx = [header.index(c) if c in header else -1 for c in cols]
        n = len(header)
        for row in reader:
            if len(row) != n:
                continue
            yield tuple(row[i] if i >= 0 else "" for i in idx)


def qnum(s: str):
    if s[:1] == "Q":
        try:
            return int(s[1:])
        except ValueError:
            return None
    return None


def as_int(s: str):
    try:
        return int(float(s)) if s else None
    except ValueError:
        return None


def _ymd(s: str):
    # "2003-08-12T00:00:00Z" -> (2003, 8, 12); BCE or malformed -> None
    if len(s) < 10 or s[4] != "-":
        return None
    try:
        return int(s[0:4]), int(s[5:7]), int(s[8:10])
    except ValueError:
        return None


def stint_seasons(start: str, end: str):
    """Seasons (labelled by the year they start in) covered by a team stint.

    Wikidata stores year-only dates as YYYY-01-01. A year-only start means "joined for the
    season starting that year", a year-only end means "left after the season ending that year".
    """
    s, e = _ymd(start), _ymd(end)
    if s is None and e is None:
        return None

    def season(ymd, is_end):
        y, m, d = ymd
        if m == 1 and d == 1:
            return y - 1 if is_end else y
        return y if m >= 7 else y - 1

    if s is not None:
        a = season(s, False)
        if e is not None:
            b = max(a, season(e, True))
        else:  # open-ended: assume still there if recent, else a single season
            b = CURRENT_SEASON if a >= CURRENT_SEASON - 6 else a
    else:
        a = b = season(e, True)
    if a < 1850 or a > CURRENT_SEASON + 1:
        return None
    return a, min(b, a + 30, CURRENT_SEASON + 1)


# ---------------------------------------------------------------- stage

def stage(raw_dir: str, stage_path: str) -> sqlite3.Connection:
    if os.path.exists(stage_path):
        os.remove(stage_path)
    db = sqlite3.connect(stage_path, isolation_level=None)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1000000; PRAGMA locking_mode=EXCLUSIVE;
        CREATE TABLE s_rel(a INTEGER, b INTEGER, type TEXT, detail INTEGER, year INTEGER);
        CREATE TABLE s_mem(fam TEXT, x INTEGER, period INTEGER, h INTEGER, role TEXT);
        CREATE TABLE s_flag(x INTEGER, flag TEXT, PRIMARY KEY(x, flag)) WITHOUT ROWID;
        CREATE TABLE s_pname(h INTEGER PRIMARY KEY, name TEXT);   -- best label: en, else mul, else any
        CREATE TABLE s_pdesc(h INTEGER PRIMARY KEY, desc TEXT);
        CREATE TABLE s_pbirth(h INTEGER PRIMARY KEY, year INTEGER);
        CREATE TABLE s_pdeath(h INTEGER PRIMARY KEY, year INTEGER);
        CREATE TABLE s_pgender(h INTEGER PRIMARY KEY, gender TEXT);
        CREATE TABLE s_psl(h INTEGER PRIMARY KEY, sl INTEGER);
        CREATE TABLE s_pimg(h INTEGER PRIMARY KEY, image TEXT);
        CREATE TABLE s_pwiki(h INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE s_alias(h INTEGER, alias TEXT);
    """)

    def load(sql, it, label):
        t0 = time.time()
        db.execute("BEGIN")
        n = 0
        batch = []
        for rec in it:
            batch.append(rec)
            if len(batch) >= 200000:
                db.executemany(sql, batch)
                n += len(batch)
                batch.clear()
        if batch:
            db.executemany(sql, batch)
            n += len(batch)
        db.execute("COMMIT")
        log(f"  {label}: {n:,} rows ({time.time() - t0:.0f}s)")

    # person -> person relations
    for d in wikidata.DIRECT:
        def gen(d=d):
            for a, b, det, yr in rows(raw_dir, d.name, "a_", "b_", "detail_", "year"):
                a, b = qnum(a), qnum(b)
                if a is None or b is None or a == b:
                    continue
                if d.swap:
                    a, b = b, a
                yield a, b, d.type, qnum(det), as_int(yr)
        load("INSERT INTO s_rel VALUES (?,?,?,?,?)", gen(), d.name)

    # person in context
    for m in wikidata.MEMBERS:
        def gen(m=m):
            if m.family == "team":
                for x, h, s, e in rows(raw_dir, m.name, "x_", "h_", "start", "end"):
                    x, h = qnum(x), qnum(h)
                    span = stint_seasons(s, e) if x and h else None
                    if span:
                        for season in range(span[0], span[1] + 1):
                            yield m.family, x, season, h, m.role
            elif m.family == "award":
                for x, h, yr in rows(raw_dir, m.name, "x_", "h_", "year"):
                    x, h, yr = qnum(x), qnum(h), as_int(yr)
                    if x and h and yr is not None:
                        yield m.family, x, yr, h, m.role
            else:
                for x, h in rows(raw_dir, m.name, "x_", "h_"):
                    x, h = qnum(x), qnum(h)
                    if x and h:
                        yield m.family, x, NOPERIOD, h, m.role
        load("INSERT INTO s_mem VALUES (?,?,?,?,?)", gen(), m.name)

    for name, flag in (("flag_music_groups", "music"), ("flag_family_groups", "family"),
                       ("flag_documentaries", "documentary")):
        load("INSERT OR IGNORE INTO s_flag VALUES (?,?)",
             ((qnum(x), flag) for (x,) in rows(raw_dir, name, "x_") if qnum(x)), name)

    # people: one value per person; the first label source to provide a name wins
    def people(name, col, conv=lambda v: v or None):
        for h, v in rows(raw_dir, name, "h_", col):
            h, v = qnum(h), conv(v)
            if h and v is not None:
                yield h, v
    for name in ("people_label_en", "people_label_mul", "people_label_any"):
        load("INSERT OR IGNORE INTO s_pname VALUES (?,?)", people(name, "name"), name)
    load("INSERT OR IGNORE INTO s_pdesc VALUES (?,?)", people("people_desc", "desc"), "people_desc")
    for name, table in (("people_birth", "s_pbirth"), ("people_death", "s_pdeath")):
        load(f"INSERT INTO {table} VALUES (?,?) ON CONFLICT(h) DO UPDATE SET year = MIN(year, excluded.year)",
             people(name, "year", as_int), name)
    load("INSERT OR IGNORE INTO s_pgender VALUES (?,?)", people("people_gender", "gender", GENDER.get), "people_gender")
    load("INSERT OR IGNORE INTO s_psl VALUES (?,?)", people("people_sitelinks", "sl", as_int), "people_sitelinks")
    load("INSERT OR IGNORE INTO s_pimg VALUES (?,?)", people("people_image", "image"), "people_image")
    load("INSERT OR IGNORE INTO s_pwiki VALUES (?,?)", people("people_enwiki", "title"), "people_enwiki")
    load("INSERT INTO s_alias VALUES (?,?)", people("people_alias", "alias"), "people_alias")
    return db


# ---------------------------------------------------------------- context names (cached lookups)

def fetch_entities(db: sqlite3.Connection, raw_dir: str, batch: int = 50000) -> None:
    """Fill s_ent (name, year, sitelinks) for every context and relation detail item.

    Results are cached per Q-id in raw/entities.db, so a rebuild only asks for new items.
    """
    cache_path = os.path.join(raw_dir, "entities.db")
    lookup_path = os.path.join(raw_dir, "_lookup.csv.gz")
    cache = sqlite3.connect(cache_path)
    cache.execute("CREATE TABLE IF NOT EXISTS ent(x INTEGER PRIMARY KEY, name TEXT, year INTEGER, sl INTEGER, "
                  "scholarly INTEGER)")
    cache.execute("CREATE TABLE IF NOT EXISTS anylabel(x INTEGER PRIMARY KEY, name TEXT)")
    db.executescript("""
        CREATE TABLE need(x INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO need SELECT x FROM ctx;
        INSERT OR IGNORE INTO need SELECT detail FROM rel_kept WHERE detail IS NOT NULL;
    """)

    def ent_row(x, got):
        name, year, sl, scholarly = got.get(x, ("", "", "", ""))
        return x, name or None, as_int(year), as_int(sl) or 0, as_int(scholarly) or 0

    def run(table, make_query, todo):
        done = 0
        stack = [todo[i:i + batch] for i in range(0, len(todo), batch)][::-1]
        while stack:
            chunk = stack.pop()
            try:
                sparql.run_query(make_query(["Q%d" % x for x in chunk]), lookup_path, log=log)
            except sparql.QueryTimeout:
                if len(chunk) > 1:  # split and retry
                    stack += [chunk[len(chunk) // 2:], chunk[:len(chunk) // 2]]
                continue
            got = {}
            with gzip.open(lookup_path, "rt", encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                width = len(next(reader))
                for r in reader:
                    if len(r) == width and qnum(r[0]):
                        got[qnum(r[0])] = r[1:]
            with cache:
                if table == "ent":
                    cache.executemany("INSERT OR REPLACE INTO ent VALUES (?,?,?,?,?)", [ent_row(x, got) for x in chunk])
                else:
                    cache.executemany("INSERT OR REPLACE INTO anylabel VALUES (?,?)",
                                      [(x, (got.get(x) or [None])[0] or None) for x in chunk])
            done += len(chunk)
            log(f"    {table}: {done:,}/{len(todo):,}")
        if os.path.exists(lookup_path):
            os.remove(lookup_path)

    cached = {x for (x,) in cache.execute("SELECT x FROM ent")}
    todo = [x for (x,) in db.execute("SELECT x FROM need") if x not in cached]
    log(f"  {len(todo):,} context/detail items to look up ({len(cached):,} cached)")
    run("ent", wikidata.entity_info, todo)
    cached_any = {x for (x,) in cache.execute("SELECT x FROM anylabel")}
    unnamed = {x for (x,) in cache.execute("SELECT x FROM ent WHERE name IS NULL")}
    todo = [x for (x,) in db.execute("SELECT x FROM need") if x in unnamed and x not in cached_any]
    log(f"  {len(todo):,} items without an English name: fetching any-language labels")
    run("anylabel", wikidata.any_labels, todo)
    cache.close()

    db.execute("ATTACH DATABASE ? AS cache", (cache_path,))
    db.executescript("""
        CREATE TABLE s_ent AS
          SELECT n.x AS x, COALESCE(e.name, a.name) AS name, e.year AS year, COALESCE(e.sl, 0) AS sl,
                 COALESCE(e.scholarly, 0) AS scholarly
          FROM need n LEFT JOIN cache.ent e ON e.x = n.x LEFT JOIN cache.anylabel a ON a.x = n.x;
        CREATE UNIQUE INDEX s_ent_x ON s_ent(x);
    """)
    db.execute("DETACH DATABASE cache")


# ---------------------------------------------------------------- assemble

SCHEMA = """
CREATE TABLE person(
  id INTEGER PRIMARY KEY,          -- dense id, ordered by prominence (0 = most Wikipedia editions)
  qid TEXT NOT NULL,               -- Wikidata id, e.g. 'Q76'
  name TEXT NOT NULL,
  description TEXT,
  birth_year INTEGER,
  death_year INTEGER,
  gender TEXT,                     -- 'm', 'f' or NULL
  sitelinks INTEGER NOT NULL DEFAULT 0,   -- Wikipedia/Wikimedia pages about them: a fame proxy
  image TEXT,                      -- Wikimedia Commons file name
  enwiki TEXT,                     -- English Wikipedia article title
  n_relations INTEGER NOT NULL DEFAULT 0, -- direct person-to-person links
  n_contexts INTEGER NOT NULL DEFAULT 0   -- films, teams, bands, papers... they belong to
);
CREATE TABLE context(
  id INTEGER PRIMARY KEY,
  qid TEXT NOT NULL,
  kind TEXT NOT NULL,              -- work, authored, group, team, election, award, event, document, org, spaceflight
  category TEXT NOT NULL,          -- see sixdeg.CATEGORIES
  name TEXT NOT NULL,
  year INTEGER,
  period INTEGER,                  -- season (team) or year (award) this context is restricted to
  sitelinks INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL            -- number of people in it
);
CREATE TABLE membership(
  person_id INTEGER NOT NULL,
  context_id INTEGER NOT NULL,
  role TEXT,                       -- e.g. 'cast member', 'director,screenwriter', 'player'
  PRIMARY KEY(person_id, context_id)
) WITHOUT ROWID;
CREATE TABLE relation(
  a_id INTEGER NOT NULL,           -- 'from' side: parent, advisor, teacher, predecessor, subject
  b_id INTEGER NOT NULL,
  type TEXT NOT NULL,              -- spouse, partner, parent, sibling, relative, stepparent, godparent,
                                   -- advisor, teacher, influence, partner_sport, significant, succession
  category TEXT NOT NULL,
  detail TEXT,                     -- office held, kinship term or role, when known
  year INTEGER
);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""

INDEXES = """
CREATE UNIQUE INDEX person_qid ON person(qid);
CREATE INDEX context_qid ON context(qid);
CREATE INDEX membership_context ON membership(context_id, person_id);
CREATE INDEX relation_a ON relation(a_id, b_id);
CREATE INDEX relation_b ON relation(b_id, a_id);
"""


def _case(column: str, mapping: dict) -> str:
    whens = " ".join("WHEN '%s' THEN '%s'" % kv for kv in mapping.items())
    return "CASE %s %s END" % (column, whens)


def _in(values) -> str:
    return "(" + ",".join("'%s'" % v for v in sorted(values)) + ")"


def assemble(db: sqlite3.Connection, out_path: str, raw_dir: str, min_isolated_sitelinks: int) -> None:
    # NB: CREATE TABLE ... AS gives expression columns (CASE, ROW_NUMBER, UNION) no type affinity, and an
    # index on such a column cannot serve lookups by an INTEGER key (full scans per row). Hence the CASTs.
    def step(label, sql):
        t0 = time.time()
        db.executescript(sql)
        log(f"  {label} ({time.time() - t0:.0f}s)")

    db.execute("CREATE TABLE caps(fam TEXT PRIMARY KEY, cap INTEGER)")
    db.executemany("INSERT INTO caps VALUES (?,?)", CAPS.items())
    step("contexts", f"""
        CREATE TABLE ctx_all AS
          SELECT fam, x, period, COUNT(DISTINCT h) AS n,
                 MAX(role IN {_in(wikidata.FILM_ROLES)}) AS film,
                 MAX(role IN {_in(wikidata.MUSIC_ROLES)}) AS music
          FROM s_mem GROUP BY fam, x, period;
        CREATE TABLE ctx AS
          SELECT CAST(ROW_NUMBER() OVER (ORDER BY c.fam, c.x, c.period) - 1 AS INTEGER) AS id, c.*
          FROM ctx_all c JOIN caps ON caps.fam = c.fam
          WHERE c.n >= 2 AND c.n <= caps.cap;
        CREATE UNIQUE INDEX ctx_key ON ctx(fam, x, period);
    """)
    step("memberships", """
        CREATE TABLE mem_kept AS
          SELECT c.id AS cid, m.h AS h, m.role AS role
          FROM s_mem m JOIN ctx c ON c.fam = m.fam AND c.x = m.x AND c.period = m.period;
    """)
    sym = _in(SYMMETRIC)
    step("relations", f"""
        CREATE TABLE rel_kept AS
          SELECT CAST(a AS INTEGER) AS a, CAST(b AS INTEGER) AS b, type, detail, MAX(year) AS year FROM (
            SELECT CASE WHEN type IN {sym} AND b < a THEN b ELSE a END AS a,
                   CASE WHEN type IN {sym} AND b < a THEN a ELSE b END AS b,
                   type, detail, year
            FROM s_rel WHERE a != b)
          GROUP BY a, b, type, detail;
        -- drop relations that cannot be true: parents 12-75 years older than the child and alive
        -- (give or take a year) at the birth; spouses, teachers, advisors and partners alive at once
        DELETE FROM rel_kept WHERE rowid IN (
          SELECT r.rowid FROM rel_kept r JOIN s_pbirth ba ON ba.h = r.a JOIN s_pbirth bb ON bb.h = r.b
          WHERE r.type = 'parent' AND (bb.year - ba.year < 12 OR bb.year - ba.year > 75));
        DELETE FROM rel_kept WHERE rowid IN (
          SELECT r.rowid FROM rel_kept r JOIN s_pdeath da ON da.h = r.a JOIN s_pbirth bb ON bb.h = r.b
          WHERE r.type = 'parent' AND bb.year > da.year + 1);
        DELETE FROM rel_kept WHERE rowid IN (
          SELECT r.rowid FROM rel_kept r
          LEFT JOIN s_pbirth ba ON ba.h = r.a LEFT JOIN s_pdeath da ON da.h = r.a
          LEFT JOIN s_pbirth bb ON bb.h = r.b LEFT JOIN s_pdeath db ON db.h = r.b
          WHERE r.type IN ('spouse', 'partner', 'advisor', 'teacher', 'partner_sport')
            AND (da.year < bb.year OR db.year < ba.year));
    """)
    log("  looking up context names")
    fetch_entities(db, raw_dir)
    acting = _in({"cast member", "voice actor", "presenter", "talk show guest"})
    step("plausibility filters", f"""
        -- when each context happened: the season / award year, else the item's earliest date
        CREATE TABLE ctx_year AS
          SELECT c.id AS id, c.fam AS fam, c.x AS x,
                 CAST(CASE WHEN c.period != {NOPERIOD} THEN c.period ELSE e.year END AS INTEGER) AS year
          FROM ctx c LEFT JOIN s_ent e ON e.x = c.x;
        CREATE UNIQUE INDEX ctx_year_id ON ctx_year(id);
        -- keep a membership only if it could be a real meeting:
        --   * events must be dated (else "commencement at Harvard" links speakers decades apart)
        --   * the person was alive then, with 2 years' grace for posthumous releases
        --     (drops archive footage of the dead, posthumous credits, editions of old texts)
        --   * on-screen "cast" of documentaries are interviewees and archive clips, not co-workers
        CREATE TABLE mem_ok AS
          SELECT mk.cid AS cid, mk.h AS h, mk.role AS role
          FROM mem_kept mk
          JOIN ctx_year cy ON cy.id = mk.cid
          LEFT JOIN s_pdeath d ON d.h = mk.h
          WHERE NOT (cy.fam = 'event' AND cy.year IS NULL)
            AND (cy.fam = 'group' OR cy.year IS NULL OR d.year IS NULL OR cy.year <= d.year + 2)
            AND NOT (mk.role IN {acting}
                     AND EXISTS (SELECT 1 FROM s_flag f WHERE f.x = cy.x AND f.flag = 'documentary'));
        --   * nobody worked with someone born after they died: in undated credits this drops the
        --     source-material member (Bach in a 1994 ballet, Aesop in a 15th-century translation)
        CREATE TABLE ctx_maxb AS
          SELECT m.cid AS cid, CAST(MAX(b.year) AS INTEGER) AS maxb
          FROM mem_ok m JOIN s_pbirth b ON b.h = m.h GROUP BY m.cid;
        CREATE UNIQUE INDEX ctx_maxb_cid ON ctx_maxb(cid);
        DELETE FROM mem_ok WHERE rowid IN (
          SELECT m.rowid FROM mem_ok m JOIN ctx_year cy ON cy.id = m.cid
          JOIN ctx_maxb x ON x.cid = m.cid JOIN s_pdeath d ON d.h = m.h
          WHERE cy.fam != 'group' AND d.year < x.maxb);
        CREATE TABLE ctx_n AS SELECT cid, COUNT(DISTINCT h) AS n FROM mem_ok GROUP BY cid;
        CREATE UNIQUE INDEX ctx_n_cid ON ctx_n(cid);
        -- renumber the surviving contexts densely (the graph arrays index them 0..C-1)
        CREATE TABLE ctx_final AS
          SELECT CAST(ROW_NUMBER() OVER (ORDER BY c.id) - 1 AS INTEGER) AS id, c.id AS old_id,
                 c.fam AS fam, c.x AS x, c.period AS period, CAST(n.n AS INTEGER) AS n, c.film AS film, c.music AS music
          FROM ctx c JOIN ctx_n n ON n.cid = c.id WHERE n.n >= 2;
        CREATE UNIQUE INDEX ctx_final_old ON ctx_final(old_id);
        CREATE TABLE mem_final AS
          SELECT f.id AS cid, m.h AS h, m.role AS role FROM mem_ok m JOIN ctx_final f ON f.old_id = m.cid;
    """)
    kept = db.execute("SELECT (SELECT COUNT(*) FROM mem_final), (SELECT COUNT(*) FROM mem_kept), "
                      "(SELECT COUNT(*) FROM ctx_final), (SELECT COUNT(*) FROM ctx)").fetchone()
    log(f"  kept {kept[0]:,} of {kept[1]:,} memberships in {kept[2]:,} of {kept[3]:,} contexts")
    step("person set", f"""
        CREATE TABLE plinks AS
          SELECT CAST(h AS INTEGER) AS h, COUNT(*) AS n FROM (
            SELECT h FROM mem_final UNION ALL SELECT a FROM rel_kept UNION ALL SELECT b FROM rel_kept)
          GROUP BY h;
        CREATE UNIQUE INDEX plinks_h ON plinks(h);
        CREATE TABLE pset AS SELECT h, n FROM plinks;
        INSERT INTO pset SELECT s.h, 0 FROM s_psl s
          WHERE s.sl >= {int(min_isolated_sitelinks)} AND NOT EXISTS (SELECT 1 FROM plinks p WHERE p.h = s.h);
        CREATE TABLE pmap AS
          SELECT ps.h AS h,
                 CAST(ROW_NUMBER() OVER (ORDER BY COALESCE(sl.sl, 0) DESC, ps.n DESC, ps.h) - 1 AS INTEGER) AS id
          FROM pset ps LEFT JOIN s_psl sl ON sl.h = ps.h;
        CREATE UNIQUE INDEX pmap_h ON pmap(h);
    """)

    if os.path.exists(out_path):
        os.remove(out_path)
    out = sqlite3.connect(out_path)
    out.executescript(SCHEMA)
    out.close()
    db.execute("ATTACH DATABASE ? AS out", (out_path,))
    group_cat = """CASE WHEN EXISTS (SELECT 1 FROM s_flag f WHERE f.x = c.x AND f.flag = 'family') THEN 'family'
                        WHEN EXISTS (SELECT 1 FROM s_flag f WHERE f.x = c.x AND f.flag = 'music') THEN 'music'
                        ELSE 'other' END"""
    work_cat = "CASE WHEN c.film THEN 'film' WHEN c.music THEN 'music' ELSE 'arts' END"
    fam_cat = _case("c.fam", FAMILY_CATEGORY)
    step("write people", """
        INSERT INTO out.person(id, qid, name, description, birth_year, death_year, gender, sitelinks, image, enwiki)
          SELECT pm.id, 'Q' || pm.h, COALESCE(n.name, 'Q' || pm.h), d.desc,
                 b.year, dd.year, g.gender, COALESCE(sl.sl, 0), img.image, w.title
          FROM pmap pm
          LEFT JOIN s_pname n ON n.h = pm.h
          LEFT JOIN s_pdesc d ON d.h = pm.h
          LEFT JOIN s_pbirth b ON b.h = pm.h
          LEFT JOIN s_pdeath dd ON dd.h = pm.h
          LEFT JOIN s_pgender g ON g.h = pm.h
          LEFT JOIN s_psl sl ON sl.h = pm.h
          LEFT JOIN s_pimg img ON img.h = pm.h
          LEFT JOIN s_pwiki w ON w.h = pm.h
          ORDER BY pm.id;
    """)
    step("write contexts", f"""
        INSERT INTO out.context(id, qid, kind, category, name, year, period, sitelinks, size)
          SELECT c.id, 'Q' || c.x, c.fam,
                 CASE c.fam WHEN 'work' THEN {work_cat} WHEN 'group' THEN {group_cat}
                            WHEN 'authored' THEN CASE WHEN e.scholarly THEN 'science' ELSE 'arts' END
                            ELSE {fam_cat} END,
                 COALESCE(e.name, 'Q' || c.x),
                 CASE WHEN c.period != {NOPERIOD} THEN c.period ELSE e.year END,
                 NULLIF(c.period, {NOPERIOD}), COALESCE(e.sl, 0), c.n
          FROM ctx_final c LEFT JOIN s_ent e ON e.x = c.x
          ORDER BY c.id;
    """)
    step("write memberships", """
        INSERT INTO out.membership(person_id, context_id, role)
          SELECT pm.id, mk.cid, GROUP_CONCAT(DISTINCT mk.role)
          FROM mem_final mk JOIN pmap pm ON pm.h = mk.h
          GROUP BY pm.id, mk.cid ORDER BY pm.id, mk.cid;
    """)
    step("write relations", f"""
        INSERT INTO out.relation(a_id, b_id, type, category, detail, year)
          SELECT pa.id, pb.id, r.type, {_case("r.type", RELATION_CATEGORY)}, e.name, r.year
          FROM rel_kept r JOIN pmap pa ON pa.h = r.a JOIN pmap pb ON pb.h = r.b
          LEFT JOIN s_ent e ON e.x = r.detail;
    """)
    db.execute("DETACH DATABASE out")


def finish_db(out_path: str, db: sqlite3.Connection) -> None:
    out = sqlite3.connect(out_path, isolation_level=None)
    out.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1000000;")
    t0 = time.time()
    out.executescript(INDEXES)
    log(f"  indexes ({time.time() - t0:.0f}s)")
    t0 = time.time()
    out.executescript("""
        BEGIN;
        UPDATE person SET n_relations = x.n FROM (
          SELECT id, COUNT(*) AS n FROM (SELECT a_id AS id FROM relation UNION ALL SELECT b_id FROM relation)
          GROUP BY id) AS x WHERE person.id = x.id;
        UPDATE person SET n_contexts = x.n FROM (
          SELECT person_id AS id, COUNT(*) AS n FROM membership GROUP BY person_id) AS x WHERE person.id = x.id;
        COMMIT;
    """)
    log(f"  link counts ({time.time() - t0:.0f}s)")

    # Full-text name search. rowid = person.id, so "ORDER BY rowid" = most prominent first.
    t0 = time.time()
    out.execute("""CREATE VIRTUAL TABLE person_search USING fts5(
        name, aliases, content='', columnsize=0, tokenize='unicode61 remove_diacritics 2', prefix='2 3')""")
    db.execute("CREATE INDEX IF NOT EXISTS s_alias_h ON s_alias(h)")
    aliases = {}
    for h, al in db.execute("SELECT a.h, group_concat(a.alias, ' ; ') FROM s_alias a "
                            "JOIN pmap pm ON pm.h = a.h GROUP BY a.h"):
        aliases["Q%d" % h] = al
    out.execute("BEGIN")
    cur = out.execute("SELECT id, qid, name FROM person")
    while True:
        chunk = cur.fetchmany(200000)
        if not chunk:
            break
        out.executemany("INSERT INTO person_search(rowid, name, aliases) VALUES (?,?,?)",
                        [(i, nm, aliases.get(q, "")) for i, q, nm in chunk])
    out.execute("COMMIT")
    out.execute("INSERT INTO person_search(person_search) VALUES ('optimize')")
    log(f"  name search index ({time.time() - t0:.0f}s)")

    counts = {k: out.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0]
              for k in ("person", "context", "membership", "relation")}
    meta = {
        "built_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "source": "Wikidata (CC0) via the QLever SPARQL endpoint " + sparql.ENDPOINT,
        **{"count_" + k: str(v) for k, v in counts.items()},
    }
    out.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta.items())
    out.execute("ANALYZE")
    out.close()
    log("  counts: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))


# ---------------------------------------------------------------- graph export

def _fetch_array(con, sql, ncols):
    cur = con.execute(sql)
    parts = []
    while True:
        chunk = cur.fetchmany(1_000_000)
        if not chunk:
            break
        parts.append(np.array(chunk, dtype=np.int64).reshape(-1, ncols))
    return np.concatenate(parts) if parts else np.zeros((0, ncols), np.int64)


def _csr(src, dst, n):
    order = np.argsort(src, kind="stable")
    ptr = np.zeros(n + 1, np.int64)
    np.cumsum(np.bincount(src, minlength=n), out=ptr[1:])
    return ptr, dst[order].astype(np.int32), order


def export_graph(db_path: str, graph_dir: str) -> None:
    t0 = time.time()
    os.makedirs(graph_dir, exist_ok=True)
    con = sqlite3.connect(db_path)
    P = con.execute("SELECT COUNT(*) FROM person").fetchone()[0]
    C = con.execute("SELECT COUNT(*) FROM context").fetchone()[0]
    cat_case = "CASE category %s END" % " ".join("WHEN '%s' THEN %d" % kv for kv in CATEGORY_CODE.items())

    sl = _fetch_array(con, "SELECT sitelinks FROM person ORDER BY id", 1)[:, 0]
    p_score = np.log1p(sl).astype(np.float32)

    ctx = _fetch_array(con, f"SELECT {cat_case}, sitelinks, size FROM context ORDER BY id", 3)
    c_cat = ctx[:, 0].astype(np.uint8)
    # Preference among equally short routes: famous contexts and small ones read best.
    c_score = (0.4 * np.log1p(ctx[:, 1]) - 0.6 * np.log1p(ctx[:, 2] / 2.0)).astype(np.float32)

    rel = _fetch_array(con, f"SELECT a_id, b_id, {cat_case} FROM relation", 3)
    src = np.concatenate([rel[:, 0], rel[:, 1]])
    dst = np.concatenate([rel[:, 1], rel[:, 0]])
    cat = np.concatenate([rel[:, 2], rel[:, 2]])
    key = np.unique((src * P + dst) * 16 + cat)          # de-duplicate (src, dst, category)
    src, dst, cat = key // 16 // P, key // 16 % P, key % 16
    d_ptr, d_nbr, order = _csr(src, dst, P)
    d_cat = cat[order].astype(np.uint8)

    mem = _fetch_array(con, "SELECT person_id, context_id FROM membership", 2)
    pc_ptr, pc_ctx, _ = _csr(mem[:, 0], mem[:, 1], P)
    cp_ptr, cp_per, _ = _csr(mem[:, 1], mem[:, 0], C)
    con.close()

    arrays = dict(p_score=p_score, c_cat=c_cat, c_score=c_score, d_ptr=d_ptr, d_nbr=d_nbr, d_cat=d_cat,
                  pc_ptr=pc_ptr, pc_ctx=pc_ctx, cp_ptr=cp_ptr, cp_per=cp_per)
    for name, arr in arrays.items():
        np.save(os.path.join(graph_dir, name + ".npy"), arr)
    with open(os.path.join(graph_dir, "meta.json"), "w") as f:
        json.dump({"persons": P, "contexts": C, "direct_edges": int(d_nbr.size // 2),
                   "memberships": int(pc_ctx.size), "categories": [k for k, _ in CATEGORIES]}, f)
    log(f"  graph: {P:,} people, {C:,} contexts, {d_nbr.size // 2:,} direct links, "
        f"{pc_ctx.size:,} memberships ({time.time() - t0:.0f}s)")


# ---------------------------------------------------------------- entry point

def reopen_stage(stage_path: str) -> sqlite3.Connection:
    """Reuse a kept stage.db: drop everything assemble() derives, keep the loaded s_* tables."""
    db = sqlite3.connect(stage_path, isolation_level=None)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-1000000;")
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 's\\_%' ESCAPE '\\'"
                              " AND name != 's_flag'").fetchall():
        db.execute(f"DROP TABLE {name}")
    db.execute("DROP TABLE IF EXISTS s_ent")
    return db


def build(data_dir: str, min_isolated_sitelinks: int = 1, keep_stage: bool = False,
          reuse_stage: bool = False) -> None:
    raw_dir = os.path.join(data_dir, "raw")
    stage_path = os.path.join(data_dir, "stage.db")
    db_path = os.path.join(data_dir, "people.db")
    tmp_db = db_path + ".building"
    t0 = time.time()
    if reuse_stage and os.path.exists(stage_path):
        log("reusing staged raw files (stage.db)")
        db = reopen_stage(stage_path)
    else:
        log("staging raw files")
        db = stage(raw_dir, stage_path)
    log("assembling database")
    assemble(db, tmp_db, raw_dir, min_isolated_sitelinks)
    log("indexing")
    finish_db(tmp_db, db)
    db.close()
    if not keep_stage:
        os.remove(stage_path)
    os.replace(tmp_db, db_path)
    log("exporting search graph")
    export_graph(db_path, os.path.join(data_dir, "graph"))
    log(f"build finished in {(time.time() - t0) / 60:.1f} min -> {db_path}")
