"""Wikipedia infobox links, MusicBrainz and dblp, loaded into the build's stage tables alongside
the Wikidata data (see build.stage).

Matching is by ids only, never by name, and only one-to-one:
  * Wikipedia infoboxes: the English Wikipedia article, which Wikidata ties to its person;
  * MusicBrainz and dblp: Wikidata's ids for that source (P434, P2456) together with the source's
    own links to Wikidata. A record whose ids point to two different Wikidata items is not merged.
People and contexts with no Wikidata item get a negative number in the stage tables and a key of
their own in s_ext / s_extc: "mb:<MusicBrainz artist id>", "dblp:<dblp person id>",
"mb:recording/<id>", "dblp:<paper key>", "wp:<Wikipedia article>".
"""
from __future__ import annotations

import csv
import gzip
import os
import re
import sqlite3
import time
import urllib.parse

from . import dbpedia
from .fetch import raw_path

NOPERIOD = -999999   # as build.NOPERIOD
# negative id ranges for things that have no Wikidata item
MB_PERSON, DBLP_PERSON = 1 << 40, 2 << 40
MB_GROUP, MB_CREDIT, DBLP_PAPER, WP_BAND = 3 << 40, 4 << 40, 5 << 40, 6 << 40

# MusicBrainz artist-to-artist relationships
MB_PERSON_LINKS = {  # (stored type, swap, detail)
    "married": ("spouse", False, None),
    "involved with": ("partner", False, None),
    "sibling": ("sibling", False, None),
    "parent": ("parent", False, None),
    "teacher": ("teacher", False, None),        # teacher -> student
    "supporting musician": ("supporting", False, None),            # supporting musician -> artist
    "vocal supporting musician": ("supporting", False, None),      # (detail: the instruments or vocals)
    "instrumental supporting musician": ("supporting", False, None),
}
MB_GROUP_ROLES = {"member of band": "member", "collaboration": "collaborator", "founder": "founder",
                  "conductor position": "conductor", "supporting musician": "supporting musician",
                  "vocal supporting musician": "supporting musician",
                  "instrumental supporting musician": "supporting musician"}
MB_GROUP_TYPES = ("Group", "Orchestra", "Choir")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def _rows(raw_dir: str, name: str):
    with gzip.open(raw_path(raw_dir, name), "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        n = len(next(reader))
        for row in reader:
            if len(row) == n:
                yield row


def _have(raw_dir: str, *names: str) -> bool:
    return all(os.path.exists(raw_path(raw_dir, n)) for n in names)


def _load(db, table: str, columns: str, it) -> int:
    db.execute(f"CREATE TABLE {table}({columns})")
    marks = ",".join("?" * len(columns.split(",")))
    n, batch = 0, []
    db.execute("BEGIN")
    for rec in it:
        batch.append(rec)
        if len(batch) >= 200_000:
            db.executemany(f"INSERT INTO {table} VALUES ({marks})", batch)
            n += len(batch)
            batch.clear()
    db.executemany(f"INSERT INTO {table} VALUES ({marks})", batch)
    db.execute("COMMIT")
    return n + len(batch)


def _year(s: str):
    return int(s[:4]) if s[:4].isdigit() and (len(s) == 4 or s[4:5] == "-") else None


def _q(s: str):
    return int(s[1:]) if s[:1] == "Q" and s[1:].isdigit() else None


def _step(db, label: str, sql: str) -> None:
    t0 = time.time()
    db.executescript(sql)
    log(f"  {label} ({time.time() - t0:.0f}s)")


def stage_sources(db: sqlite3.Connection, raw_dir: str, new_people: bool = True) -> None:
    """Add the other sources to s_rel / s_mem (and their people to the s_p* tables).
    new_people: also add people who are in MusicBrainz or dblp but not in Wikidata."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS s_ext(h INTEGER PRIMARY KEY, key TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS s_extc(x INTEGER PRIMARY KEY, key TEXT NOT NULL, name TEXT, year INTEGER,
                                          category TEXT);
        CREATE INDEX IF NOT EXISTS s_pwiki_title ON s_pwiki(title);
    """)
    if _have(raw_dir, "dbp_spouse", "dbp_band_members", "dbp_band_wikidata"):
        _wikipedia(db, raw_dir)
    if _have(raw_dir, "mb_artists", "mb_links", "mb_credits", "mb_aliases"):
        _musicbrainz(db, raw_dir, new_people)
    if _have(raw_dir, "dblp_people", "dblp_names", "dblp_papers", "dblp_authors"):
        _dblp(db, raw_dir, new_people)
    db.executescript("DROP TABLE IF EXISTS t_known; DROP TABLE IF EXISTS t_map;")


def _known(db) -> None:
    """t_known: every Wikidata human (anyone with a label)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 't_known'").fetchone():
        db.execute("CREATE TABLE t_known AS SELECT h FROM s_pname WHERE h > 0")
        db.execute("CREATE UNIQUE INDEX t_known_h ON t_known(h)")


# ---------------------------------------------------------------- Wikipedia infoboxes (DBpedia)

def _title(resource: str) -> str:
    return urllib.parse.unquote(resource).replace("_", " ")


def _wikipedia(db, raw_dir: str) -> None:
    t0 = time.time()

    def links():
        for link in dbpedia.LINKS:
            if _have(raw_dir, link.name):
                for a, b in _rows(raw_dir, link.name):
                    yield _title(a), _title(b), link.type, int(link.swap), link.detail or None
    n = _load(db, "t_wp", "a, b, type, swap, detail", links())
    db.executescript(f"""
        INSERT INTO s_rel(a, b, type, detail, year, detail_text, source)
          SELECT CASE WHEN t.swap THEN pb.h ELSE pa.h END, CASE WHEN t.swap THEN pa.h ELSE pb.h END,
                 t.type, NULL, NULL, t.detail, 'wikipedia'
          FROM t_wp t JOIN s_pwiki pa ON pa.title = t.a JOIN s_pwiki pb ON pb.title = t.b
          WHERE pa.h != pb.h;
        DROP TABLE t_wp;
    """)
    # bands: a band keeps its Wikidata number when DBpedia links it to exactly one item
    _load(db, "t_wpbq", "band, q", ((_title(x), _q(q)) for x, q in _rows(raw_dir, "dbp_band_wikidata") if _q(q)))
    _load(db, "t_wpbm", "band, member", ((_title(x), _title(h)) for x, h in _rows(raw_dir, "dbp_band_members")))
    db.executescript(f"""
        CREATE TABLE t_wpband AS
          SELECT band, CASE WHEN (SELECT COUNT(DISTINCT q) FROM t_wpbq q WHERE q.band = b.band) = 1
                            THEN (SELECT q FROM t_wpbq q WHERE q.band = b.band) END AS q
          FROM (SELECT DISTINCT band FROM t_wpbm) b;
        CREATE UNIQUE INDEX t_wpband_band ON t_wpband(band);
        INSERT INTO s_extc(x, key, name, year, category)
          SELECT -({WP_BAND} + rowid), 'wp:' || band, band, NULL, 'music' FROM t_wpband WHERE q IS NULL;
        INSERT INTO s_mem(fam, x, period, h, role, source)
          SELECT 'group', COALESCE(b.q, -({WP_BAND} + b.rowid)), {NOPERIOD}, p.h, 'member', 'wikipedia'
          FROM t_wpbm m JOIN t_wpband b ON b.band = m.band JOIN s_pwiki p ON p.title = m.member;
        INSERT OR IGNORE INTO s_flag SELECT q, 'music' FROM t_wpband WHERE q IS NOT NULL;
        DROP TABLE t_wpbq; DROP TABLE t_wpbm; DROP TABLE t_wpband;
    """)
    # display names: "Genesis (band)" -> "Genesis"
    db.executemany("UPDATE s_extc SET name = ? WHERE x = ?",
                   [(re.sub(r" \([^()]*\)$", "", name), x) for x, name in
                    db.execute(f"SELECT x, name FROM s_extc WHERE key LIKE 'wp:%'").fetchall()])
    log(f"  Wikipedia infoboxes: {n:,} links read ({time.time() - t0:.0f}s)")


# ---------------------------------------------------------------- MusicBrainz

def _one_to_one(db, pairs: str, out: str, key: str) -> None:
    """out(key PRIMARY KEY, q): keys whose ids point to exactly one Wikidata item."""
    db.executescript(f"""
        CREATE TABLE {out} AS SELECT {key}, MIN(q) AS q FROM {pairs} GROUP BY {key} HAVING COUNT(DISTINCT q) = 1;
        CREATE UNIQUE INDEX {out}_k ON {out}({key});
    """)


def _musicbrainz(db, raw_dir: str, new_people: bool) -> None:
    t0 = time.time()
    _known(db)
    _load(db, "t_mba", "mbid, type, name, gender, birth, death, dis, country",
          ((r[0], r[1], r[2], r[3], _year(r[4]), _year(r[5]), r[6], r[7]) for r in _rows(raw_dir, "mb_artists")))
    db.execute("CREATE UNIQUE INDEX t_mba_id ON t_mba(mbid)")

    def wd():
        for r in _rows(raw_dir, "mb_artists"):
            for q in filter(None, r[8].split("|")):
                if _q(q):
                    yield r[0], _q(q)
        for x, mbid in _rows(raw_dir, "ext_musicbrainz"):
            if _q(x):
                yield mbid, _q(x)
    _load(db, "t_mbwd", "mbid, q", wd())
    _one_to_one(db, "t_mbwd", "t_mbmap", "mbid")
    keep_new = "1" if new_people else "0"
    _step(db, "MusicBrainz people", f"""
        -- people: their Wikidata number when it is a known person, else (optionally) a new one
        CREATE TABLE t_mbp AS
          SELECT a.mbid AS mbid, CASE WHEN k.h IS NOT NULL THEN k.h WHEN {keep_new} THEN -({MB_PERSON} + a.rowid) END AS h,
                 k.h IS NULL AS new
          FROM t_mba a LEFT JOIN t_mbmap m ON m.mbid = a.mbid LEFT JOIN t_known k ON k.h = m.q
          WHERE a.type = 'Person';
        DELETE FROM t_mbp WHERE h IS NULL;
        CREATE UNIQUE INDEX t_mbp_id ON t_mbp(mbid);
        CREATE TABLE t_mbg AS
          SELECT a.mbid AS mbid, COALESCE(m.q, -({MB_GROUP} + a.rowid)) AS x, m.q IS NULL AS new, a.name AS name
          FROM t_mba a LEFT JOIN t_mbmap m ON m.mbid = a.mbid WHERE a.type IN {MB_GROUP_TYPES};
        CREATE UNIQUE INDEX t_mbg_id ON t_mbg(mbid);
    """)
    _load(db, "t_mbl", "a, b, type, begin, attrs", ((r[0], r[1], r[2], _year(r[3]), r[5]) for r in _rows(raw_dir, "mb_links")))
    rel = [(t, k[0], int(k[1]), k[2]) for t, k in MB_PERSON_LINKS.items()]
    grp = list(MB_GROUP_ROLES.items())
    db.execute("CREATE TABLE t_mbrel(mb TEXT, type TEXT, swap INTEGER, detail TEXT)")
    db.executemany("INSERT INTO t_mbrel VALUES (?,?,?,?)", rel)
    db.execute("CREATE TABLE t_mbrole(mb TEXT, role TEXT)")
    db.executemany("INSERT INTO t_mbrole VALUES (?,?)", grp)
    step = "('|' || COALESCE(l.attrs, '') || '|' LIKE '%|step|%')"   # a stepparent, a step-sibling
    _step(db, "MusicBrainz relationships", f"""
        INSERT INTO s_rel(a, b, type, detail, year, detail_text, source)
          SELECT CASE WHEN r.swap THEN pb.h ELSE pa.h END, CASE WHEN r.swap THEN pa.h ELSE pb.h END,
                 CASE WHEN r.type = 'parent' AND {step} THEN 'stepparent'
                      WHEN r.type = 'sibling' AND {step} THEN 'relative' ELSE r.type END,
                 NULL, CASE WHEN r.type = 'spouse' THEN l.begin END,
                 CASE WHEN r.type = 'supporting' THEN NULLIF(REPLACE(l.attrs, '|', ', '), '')
                      WHEN r.type = 'sibling' AND {step} THEN 'step-sibling' END, 'musicbrainz'
          FROM t_mbl l JOIN t_mbrel r ON r.mb = l.type JOIN t_mbp pa ON pa.mbid = l.a JOIN t_mbp pb ON pb.mbid = l.b
          WHERE pa.h != pb.h;
        INSERT INTO s_mem(fam, x, period, h, role, source)
          SELECT 'group', g.x, {NOPERIOD}, p.h, r.role, 'musicbrainz'
          FROM t_mbl l JOIN t_mbrole r ON r.mb = l.type JOIN t_mbp p ON p.mbid = l.a JOIN t_mbg g ON g.mbid = l.b;
        INSERT OR IGNORE INTO s_flag SELECT x, 'music' FROM t_mbg WHERE NOT new;
        DROP TABLE t_mbl; DROP TABLE t_mbrel; DROP TABLE t_mbrole;
    """)
    n = _load(db, "t_mbc", "kind, target, title, year, artist, role",
              ((r[0], r[1], r[2], _year(r[3]), r[4], r[5]) for r in _rows(raw_dir, "mb_credits")))
    _step(db, f"MusicBrainz credits ({n:,})", f"""
        CREATE TABLE t_mbcp AS
          SELECT c.kind AS kind, c.target AS target, p.h AS h, c.role AS role
          FROM t_mbc c JOIN t_mbp p ON p.mbid = c.artist;
        -- recordings, songs and albums that two or more people worked on
        CREATE TABLE t_mbt AS
          SELECT kind, target FROM t_mbcp GROUP BY kind, target HAVING COUNT(DISTINCT h) >= 2;
        CREATE UNIQUE INDEX t_mbt_k ON t_mbt(kind, target);
        CREATE TABLE t_mbtitle AS SELECT kind, target, MAX(title) AS title, MIN(year) AS year FROM t_mbc
          WHERE (kind, target) IN (SELECT kind, target FROM t_mbt) GROUP BY kind, target;
        CREATE UNIQUE INDEX t_mbtitle_k ON t_mbtitle(kind, target);
        INSERT INTO s_extc(x, key, name, year, category)
          SELECT -({MB_CREDIT} + t.rowid), 'mb:' || t.kind || '/' || t.target, n.title, n.year, 'music'
          FROM t_mbt t JOIN t_mbtitle n ON n.kind = t.kind AND n.target = t.target;
        INSERT INTO s_mem(fam, x, period, h, role, source)
          SELECT 'work', -({MB_CREDIT} + t.rowid), {NOPERIOD}, c.h, c.role, 'musicbrainz'
          FROM t_mbcp c JOIN t_mbt t ON t.kind = c.kind AND t.target = c.target;
        INSERT INTO s_extc(x, key, name, year, category)
          SELECT g.x, 'mb:' || g.mbid, g.name, NULL, 'music' FROM t_mbg g WHERE g.new;
    """)
    # the new people: names, years, gender, a description from MusicBrainz, aliases for search
    credits = dict(db.execute("SELECT h, COUNT(DISTINCT kind || target) FROM t_mbcp WHERE h < 0 GROUP BY h"))
    people = db.execute("SELECT p.h, a.mbid, a.name, a.gender, a.birth, a.death, a.dis FROM t_mbp p "
                        "JOIN t_mba a ON a.mbid = p.mbid WHERE p.new").fetchall()
    db.execute("BEGIN")
    db.executemany("INSERT INTO s_ext VALUES (?,?)", [(h, "mb:" + mbid) for h, mbid, *_ in people])
    db.executemany("INSERT OR IGNORE INTO s_pname VALUES (?,?)", [(h, name) for h, _, name, *_ in people])
    db.executemany("INSERT OR IGNORE INTO s_pdesc VALUES (?,?)", [
        (h, " · ".join(filter(None, [dis, f"{credits[h]:,} MusicBrainz credit{'s' if credits[h] != 1 else ''}"
                                     if credits.get(h) else "MusicBrainz artist"])))
        for h, _, _, _, _, _, dis in people])
    db.executemany("INSERT OR IGNORE INTO s_pbirth VALUES (?,?)", [(h, b) for h, _, _, _, b, _, _ in people if b])
    db.executemany("INSERT OR IGNORE INTO s_pdeath VALUES (?,?)", [(h, d) for h, _, _, _, _, d, _ in people if d])
    db.executemany("INSERT OR IGNORE INTO s_pgender VALUES (?,?)",
                   [(h, {"Male": "m", "Female": "f"}[g]) for h, _, _, g, *_ in people if g in ("Male", "Female")])
    db.execute("COMMIT")
    _load(db, "t_mbal", "mbid, alias", _rows(raw_dir, "mb_aliases"))
    db.executescript("""
        INSERT INTO s_alias SELECT p.h, a.alias FROM t_mbal a JOIN t_mbp p ON p.mbid = a.mbid WHERE p.new;
        DROP TABLE t_mbal; DROP TABLE t_mbc; DROP TABLE t_mbcp; DROP TABLE t_mbt; DROP TABLE t_mbtitle;
        DROP TABLE t_mba; DROP TABLE t_mbwd; DROP TABLE t_mbmap; DROP TABLE t_mbp; DROP TABLE t_mbg;
    """)
    log(f"  MusicBrainz: {len(people):,} people not in Wikidata ({time.time() - t0:.0f}s)")


# ---------------------------------------------------------------- dblp

def _dblp(db, raw_dir: str, new_people: bool) -> None:
    t0 = time.time()
    _known(db)
    _load(db, "t_dp", "pid, name, aff, papers", ((r[0], r[1], r[2], int(r[3] or 0)) for r in _rows(raw_dir, "dblp_people")))
    db.execute("CREATE UNIQUE INDEX t_dp_pid ON t_dp(pid)")

    def wd():
        for r in _rows(raw_dir, "dblp_people"):
            for q in filter(None, r[4].split("|")):
                if _q(q):
                    yield r[0], _q(q)
        for h, pid in _rows(raw_dir, "ext_dblp"):
            if _q(h):
                yield pid, _q(h)
    _load(db, "t_dwd", "pid, q", wd())
    _one_to_one(db, "t_dwd", "t_dmap", "pid")
    keep_new = "1" if new_people else "0"
    db.executescript(f"""
        CREATE TABLE t_dpp AS
          SELECT d.pid AS pid, CASE WHEN k.h IS NOT NULL THEN k.h WHEN {keep_new} THEN -({DBLP_PERSON} + d.rowid) END AS h,
                 k.h IS NULL AS new
          FROM t_dp d LEFT JOIN t_dmap m ON m.pid = d.pid LEFT JOIN t_known k ON k.h = m.q;
        DELETE FROM t_dpp WHERE h IS NULL;
        CREATE UNIQUE INDEX t_dpp_pid ON t_dpp(pid);
    """)
    _load(db, "t_dn", "name, pid", _rows(raw_dir, "dblp_names"))
    # papers list their authors by name, and each name is one person's (dblp numbers namesakes);
    # matching them here rather than in SQL keeps only the people being kept, and is much faster
    names = dict(db.execute("SELECT n.name, p.h FROM t_dn n JOIN t_dpp p ON p.pid = n.pid"))
    n = _load(db, "t_dah", "key, h", ((k, names[nm]) for k, nm in _rows(raw_dir, "dblp_authors") if nm in names))
    del names
    # without dblp's own people, only the papers of people on Wikidata matter (a few % of them)
    keys = None if new_people else {k for (k,) in db.execute("SELECT DISTINCT key FROM t_dah")}
    _load(db, "t_dpap", "key, title, year", ((k, t, _year(y)) for k, t, y in _rows(raw_dir, "dblp_papers")
                                            if keys is None or k in keys))
    db.execute("CREATE UNIQUE INDEX t_dpap_key ON t_dpap(key)")
    _step(db, f"dblp authorships ({n:,})", f"""
        CREATE TABLE t_dk AS SELECT key FROM t_dah GROUP BY key HAVING COUNT(DISTINCT h) >= 2;
        CREATE UNIQUE INDEX t_dk_key ON t_dk(key);
        INSERT INTO s_extc(x, key, name, year, category)
          SELECT -({DBLP_PAPER} + p.rowid), 'dblp:' || p.key, p.title, p.year, 'science'
          FROM t_dpap p JOIN t_dk k ON k.key = p.key;
        INSERT INTO s_mem(fam, x, period, h, role, source)
          SELECT DISTINCT 'authored', -({DBLP_PAPER} + p.rowid), {NOPERIOD}, a.h, 'author', 'dblp'
          FROM t_dah a JOIN t_dk k ON k.key = a.key JOIN t_dpap p ON p.key = a.key;
    """)
    people = db.execute("SELECT p.h, d.pid, d.name, d.aff, d.papers FROM t_dpp p JOIN t_dp d ON d.pid = p.pid "
                        "WHERE p.new").fetchall()
    db.execute("BEGIN")
    db.executemany("INSERT INTO s_ext VALUES (?,?)", [(h, "dblp:" + pid) for h, pid, *_ in people])
    db.executemany("INSERT OR IGNORE INTO s_pname VALUES (?,?)", [(h, name) for h, _, name, *_ in people])
    db.executemany("INSERT OR IGNORE INTO s_pdesc VALUES (?,?)", [
        (h, " · ".join(filter(None, [aff, f"{n:,} computer science publication{'s' if n != 1 else ''}"])))
        for h, _, _, aff, n in people])
    db.execute("COMMIT")
    db.executescript("""
        DROP TABLE t_dp; DROP TABLE t_dwd; DROP TABLE t_dmap; DROP TABLE t_dpp; DROP TABLE t_dn;
        DROP TABLE t_dpap; DROP TABLE t_dah; DROP TABLE t_dk;
    """)
    log(f"  dblp: {len(people):,} people not in Wikidata ({time.time() - t0:.0f}s)")
