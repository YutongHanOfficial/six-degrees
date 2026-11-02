"""Read side of people.db: name search, person cards, and plain-English explanations of links."""
from __future__ import annotations

import re
import sqlite3
import threading
import urllib.parse

from . import CATEGORIES

CATEGORY_LABEL = dict(CATEGORIES)
RELATION_PRIORITY = ["spouse", "parent", "sibling", "partner", "relative", "stepparent", "godparent",
                     "advisor", "teacher", "academic_advisor", "coach", "supporting", "partner_sport",
                     "deputy", "served_during", "succession", "associated", "significant", "influence"]
SOURCE_LABEL = {"wikidata": "Wikidata", "wikipedia": "Wikipedia", "musicbrainz": "MusicBrainz", "dblp": "dblp"}
_TOKEN = re.compile(r"\w+", re.UNICODE)
# "(1935–1977)", "(born 2002)", "(b. 1990)" at the end of a description: redundant next to the years
_YEARS_SUFFIX = re.compile(r"\s*\((?:born |b\. )?-?\d{1,4}(?:\s*[–-]\s*-?\d{1,4})?\)\s*$")
_QID = re.compile(r"^[Qq](\d+)$")
_KEY = re.compile(r"^(mb|dblp):\S+$")   # people who are only in MusicBrainz or dblp


def source_url(key: str):
    """The page of a person ('Q76', 'mb:<id>', 'dblp:<pid>') or context ('mb:recording/<id>',
    'dblp:<paper key>', 'wp:<article>') in the database it comes from."""
    if key.startswith("Q"):
        return "https://www.wikidata.org/wiki/" + key
    src, _, rest = key.partition(":")
    if src == "mb":
        return "https://musicbrainz.org/" + (rest if "/" in rest else "artist/" + rest)
    if src == "dblp":
        return ("https://dblp.org/rec/" + rest + ".html") if rest.split("/")[0] in DBLP_VENUES else \
            "https://dblp.org/pid/" + rest + ".html"
    if src == "wp":
        return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(rest.replace(" ", "_"))
    return None


DBLP_VENUES = {"journals", "conf", "books", "series", "reference", "phd", "persons", "tr", "ms", "www", "dblpnote"}


def image_url(file_name, width=160):
    if not file_name:
        return None
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{file_name}?width={width}"


def _gendered(gender, male, female, neutral):
    return male if gender == "m" else female if gender == "f" else neutral


_NEEDS_ARTICLE = re.compile(
    r"\b(elections?|conference|battle|siege|war|revolution|uprising|treaty|agreement|accords?|pact|act|"
    r"declaration|charter|constitution|manifesto|petition|resolution|convention|congress|council|summit|"
    r"assembly|parliament|senate|committee|commission|society|academy|club|party|group|family|dynasty|house|"
    r"games|olympics|cup|championships?|tournament|final|race|marathon|grand prix|open|trophy|league|"
    r"award|prize|medal|order|cross|honou?rs|expedition|festival|referendum|debate|primary|caucus|"
    r"orchestra|choir|ensemble|quartet|philharmonic)\b", re.I)


def _the(name: str) -> str:
    """'the Yalta Conference', 'the 1972 Canadian federal election', but 'Live Aid' / 'The Inklings'."""
    if name.lower().startswith("the "):
        return name
    if name[:1].isdigit() or _NEEDS_ARTICLE.search(name):
        return "the " + name
    return name


def _years(p):
    b, d = p.get("birth_year"), p.get("death_year")
    if b is None and d is None:
        return ""
    fmt = lambda y: f"{-y} BC" if y < 0 else str(y)
    if d is None:
        return f"b. {fmt(b)}"
    return f"{fmt(b) if b is not None else '?'}–{fmt(d)}"


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()

    @property
    def db(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            uri = "file:" + urllib.parse.quote(self.db_path) + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, check_same_thread=False)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    # ------------------------------------------------------------ people
    def stats(self) -> dict:
        meta = {k: v for k, v in self.db.execute("SELECT key, value FROM meta")}
        return {
            "people": int(meta.get("count_person", 0)),
            "contexts": int(meta.get("count_context", 0)),
            "memberships": int(meta.get("count_membership", 0)),
            "relations": int(meta.get("count_relation", 0)),
            "built_at": meta.get("built_at"),
            "source": meta.get("source"),
        }

    def card(self, row) -> dict:
        p = dict(row)
        p["years"] = _years(p)
        if p["years"] and p.get("description"):
            p["description"] = _YEARS_SUFFIX.sub("", p["description"]) or p["description"]
        p["image_url"] = image_url(p.get("image"))
        p["wikipedia"] = ("https://en.wikipedia.org/wiki/" + urllib.parse.quote(p["enwiki"].replace(" ", "_"))
                          if p.get("enwiki") else None)
        p["wikidata"] = source_url(p["qid"]) if p["qid"].startswith("Q") else None
        # people who are only in MusicBrainz or dblp: their page there
        p["source"], p["link"] = (("Wikidata", None) if p["qid"].startswith("Q") else
                                  (SOURCE_LABEL["musicbrainz" if p["qid"].startswith("mb:") else "dblp"],
                                   source_url(p["qid"])))
        return p

    def people(self, ids) -> dict:
        ids = list(dict.fromkeys(int(i) for i in ids))
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = "SELECT * FROM person WHERE id IN (%s)" % ",".join("?" * len(chunk))
            for row in self.db.execute(q, chunk):
                out[row["id"]] = self.card(row)
        return out

    def person_by_qid(self, qid: str):
        """By Wikidata id ('Q76') or, for people only in another source, its key ('mb:<id>')."""
        qid = qid.strip()
        key = qid.upper() if _QID.match(qid) else qid
        row = self.db.execute("SELECT * FROM person WHERE qid = ?", (key,)).fetchone()
        return self.card(row) if row else None

    def search(self, text: str, limit: int = 8) -> list:
        text = (text or "").strip()
        if not text:
            return []
        if _QID.match(text) or _KEY.match(text):
            p = self.person_by_qid(text)
            return [p] if p else []
        tokens = _TOKEN.findall(text.lower())
        if not tokens:
            return []
        # every token must match; the last one may be a prefix (search-as-you-type), unless it is a
        # single character, which would expand to a huge slice of the index
        last = '"%s"*' % tokens[-1] if len(tokens[-1]) > 1 else '"%s"' % tokens[-1]
        expr = " ".join(['"%s"' % t for t in tokens[:-1]] + [last])
        ids = [r[0] for r in self.db.execute(
            "SELECT rowid FROM person_search WHERE person_search MATCH ? ORDER BY rowid LIMIT ?",
            (expr, int(limit)))]
        cards = self.people(ids)
        return [cards[i] for i in ids if i in cards]

    def resolve(self, text: str):
        hits = self.search(text, 1)
        return hits[0] if hits else None

    def random_pair(self, pool: int = 3000):
        rows = self.db.execute(
            "SELECT id FROM person WHERE id < ? AND n_relations + n_contexts > 0 ORDER BY RANDOM() LIMIT 2",
            (pool,)).fetchall()
        cards = self.people([r[0] for r in rows])
        return [cards[r[0]] for r in rows]

    # ------------------------------------------------------------ links
    def explain(self, people: list, links: list, cats: set, cards: dict) -> list:
        return [self.explain_link(people[i], people[i + 1], via, cats, cards) for i, via in enumerate(links)]

    def explain_link(self, a: int, b: int, via: int, cats: set, cards: dict) -> dict:
        A, B = cards[a], cards[b]
        shared = self.shared(a, b, cats)
        if via >= 0:
            ctx = dict(self.db.execute("SELECT * FROM context WHERE id = ?", (via,)).fetchone())
            mem = {r["person_id"]: r for r in self.db.execute(
                "SELECT person_id, role, source FROM membership WHERE context_id = ? AND person_id IN (?, ?)",
                (via, a, b))}
            roles = {p: set((r["role"] or "").split(",")) for p, r in mem.items()}
            main = next((g for g in shared if via in g["ids"]), None)
            title = main["title"] if main else context_title(ctx)
            out = {"kind": "context", "category": ctx["category"],
                   "sentence": context_sentence(ctx, A, B, roles.get(a, set()), roles.get(b, set()), title),
                   "context": {k: ctx[k] for k in ("qid", "name", "year", "kind", "period", "size")},
                   "sources": _sources(*(r["source"] for r in mem.values()))}
            # the record to check it against: the context's own page, unless that is a Wikidata item
            # and Wikidata isn't among the sources
            out["url"] = (None if ctx["qid"].startswith("Q") and "Wikidata" not in out["sources"]
                          else source_url(ctx["qid"]))
            shared = [g for g in shared if g is not main]
        else:
            rels = [dict(r) for r in self.db.execute(
                "SELECT * FROM relation WHERE (a_id = ? AND b_id = ?) OR (a_id = ? AND b_id = ?)", (a, b, b, a))]
            rels.sort(key=lambda r: (r["category"] not in cats, RELATION_PRIORITY.index(r["type"])))
            rel = rels[0]
            out = {"kind": "relation", "category": rel["category"], "type": rel["type"],
                   "sentence": relation_sentence(rel, A, B), "sources": _sources(rel["source"]), "url": None}
        out["category_label"] = CATEGORY_LABEL[out["category"]]
        out["also"] = {"count": len(shared), "items": [g["title"] for g in shared[:4]]}
        return out

    def shared(self, a: int, b: int, cats: set) -> list:
        """Everything two people were part of together, best known first. Seasons at the same
        team and years of the same award are merged: 'Villarreal CF (2021–24)'."""
        groups = {}
        for r in self.db.execute("""
                SELECT c.id, c.qid, c.name, c.year, c.kind, c.category, c.period FROM membership m1
                JOIN membership m2 ON m2.person_id = ? AND m2.context_id = m1.context_id
                JOIN context c ON c.id = m1.context_id
                WHERE m1.person_id = ? ORDER BY c.sitelinks DESC, c.id""", (b, a)):
            if r["category"] not in cats:
                continue
            key = (r["kind"], r["qid"]) if r["kind"] in ("team", "award") else r["id"]
            g = groups.setdefault(key, {"row": dict(r), "ids": [], "periods": []})
            g["ids"].append(r["id"])
            if r["period"] is not None:
                g["periods"].append(r["period"])
        out = []
        for g in groups.values():
            r, periods = g["row"], sorted(set(g["periods"]))
            if r["kind"] == "team" and periods:
                g["title"] = f"{r['name']} ({season_ranges(periods)})"
            elif r["kind"] == "award" and periods:
                g["title"] = f"{r['name']} ({', '.join(str(p) for p in periods)})"
            else:
                g["title"] = context_title(r)
            out.append(g)
        return out

    # ------------------------------------------------------------ ego network
    def neighborhood(self, pid: int, per_group: int = 12) -> dict:
        db = self.db
        rels = [dict(r) for r in db.execute("""
            SELECT r.*, r.b_id AS other, 'out' AS dir FROM relation r WHERE r.a_id = ?
            UNION ALL SELECT r.*, r.a_id AS other, 'in' AS dir FROM relation r WHERE r.b_id = ?""", (pid, pid))]
        cards = self.people([r["other"] for r in rels] + [pid])
        me = cards[pid]
        groups = {}
        for r in sorted(rels, key=lambda r: (RELATION_PRIORITY.index(r["type"]), r["other"])):
            other = cards[r["other"]]
            a, b = (me, other) if r["dir"] == "out" else (other, me)
            groups.setdefault(r["category"], []).append({
                "person": _mini(other), "sentence": relation_sentence(r, a, b)})
        # contexts, with a team's seasons (and an award's years) folded into one entry
        by_cat = {}
        for c in db.execute("""
                SELECT c.*, m.role FROM membership m JOIN context c ON c.id = m.context_id
                WHERE m.person_id = ? ORDER BY c.sitelinks DESC, c.year DESC""", (pid,)):
            key = (c["kind"], c["qid"]) if c["kind"] in ("team", "award") else c["id"]
            g = by_cat.setdefault(c["category"], {}).setdefault(
                key, {"row": dict(c), "ids": [], "periods": [], "roles": set(), "size": 0})
            g["ids"].append(c["id"])
            g["roles"].update(r for r in (c["role"] or "").split(",") if r)
            g["size"] = max(g["size"], c["size"])
            if c["period"] is not None:
                g["periods"].append(c["period"])
        contexts = {}
        for cat, folded in by_cat.items():
            shown = []
            for g in list(folded.values())[:per_group]:
                c, periods = g["row"], sorted(set(g["periods"]))
                if c["kind"] == "team" and periods:
                    title = f"{c['name']} ({season_ranges(periods)})"
                elif c["kind"] == "award" and periods:
                    title = f"{c['name']} ({', '.join(map(str, periods))})"
                else:
                    title = context_title(c)
                marks = ",".join("?" * len(g["ids"]))
                mates = [r[0] for r in db.execute(
                    f"SELECT DISTINCT person_id FROM membership WHERE context_id IN ({marks}) AND person_id != ? "
                    "ORDER BY person_id LIMIT 4", (*g["ids"], pid))]
                mate_cards = self.people(mates)
                shown.append({"title": title, "role": ", ".join(sorted(g["roles"])), "size": g["size"],
                              "seasons": len(periods) if c["kind"] == "team" else None,
                              "with": [_mini(mate_cards[m]) for m in mates if m in mate_cards]})
            contexts[cat] = {"total": len(folded), "items": shown}
        return {"person": me, "relations": groups, "contexts": contexts,
                "labels": CATEGORY_LABEL}


def _mini(p: dict) -> dict:
    return {k: p.get(k) for k in ("qid", "name", "description", "years", "image_url")}


def _sources(*values) -> list:
    """'wikidata,musicbrainz' (one value per membership/relation) -> ['Wikidata', 'MusicBrainz']"""
    seen = dict.fromkeys(x for v in values for x in (v or "").split(",") if x)
    return [SOURCE_LABEL[x] for x in SOURCE_LABEL if x in seen]


# ---------------------------------------------------------------- sentences

def context_title(c: dict) -> str:
    name = c["name"]
    if c["kind"] == "team" and c.get("period") is not None:
        return f"{name} ({season_ranges([c['period']])})"
    if c["kind"] == "award" and c.get("period") is not None:
        return f"{name} ({c['period']})"
    if c.get("year") and c["kind"] in ("work", "authored", "election", "event", "document", "spaceflight", "org"):
        if str(c["year"]) not in name:
            return f"{name} ({c['year']})"
    return name


def season_ranges(seasons) -> str:
    """[2021, 2022, 2023, 2026] -> '2021–24, 2026–27' (seasons are labelled by their first year)."""
    runs, out = [], []
    for s in sorted(set(seasons)):
        if runs and s == runs[-1][1] + 1:
            runs[-1][1] = s
        else:
            runs.append([s, s])
    for a, b in runs:
        end = b + 1
        out.append(f"{a}–{end % 100:02d}" if end // 100 == a // 100 else f"{a}–{end}")
    return ", ".join(out)


def context_sentence(ctx: dict, A: dict, B: dict, ra: set, rb: set, title: str = None) -> str:
    a, b = A["name"], B["name"]
    kind, cat = ctx["kind"], ctx["category"]
    title = title or context_title(ctx)
    when = title[len(ctx["name"]):].strip() if title.startswith(ctx["name"]) else ""
    if kind == "work":
        acting = {"cast member", "voice actor", "presenter", "talk show guest"}
        if cat == "film":
            if ra & acting and rb & acting:
                return f"{a} and {b} both appeared in {title}"
            if "director" in ra and rb & acting:
                return f"{a} directed {b} in {title}"
            if "director" in rb and ra & acting:
                return f"{b} directed {a} in {title}"
        if cat == "music" and ra & _PERFORM and rb & _PERFORM:
            return f"{a} and {b} both performed on {title}"
        if "lyricist" in ra and "composer" in rb and not ra & _MUSIC_WRITING - {"lyricist"}:
            return f"{a} wrote the words and {b} the music of {title}"
        if "lyricist" in rb and "composer" in ra and not rb & _MUSIC_WRITING - {"lyricist"}:
            return f"{b} wrote the words and {a} the music of {title}"
        if ra & _WRITING and rb & _WRITING:
            return f"{a} and {b} co-wrote {title}"
        return f"{a} ({_role(ra)}) and {b} ({_role(rb)}) both worked on {title}"
    if kind == "authored":
        return f"{a} and {b} co-authored “{ctx['name']}”" + (f" ({ctx['year']})" if ctx.get("year") else "")
    if kind == "group":
        band = {"member", "founder"}
        if cat == "music" and ra & band and rb & band:
            return f"{a} and {b} were bandmates in {ctx['name']}"
        if cat == "music" and "collaborator" in ra and "collaborator" in rb:   # a duo's joint credit
            return f"{a} and {b} performed together as {ctx['name']}"
        if cat == "music":
            return f"{a} ({_role(ra)}) and {b} ({_role(rb)}) both played in {_the(ctx['name'])}"
        if cat == "family":
            return f"{a} and {b} are both members of {_the(ctx['name'])}"
        return f"{a} and {b} were both members of {_the(ctx['name'])}"
    if kind == "team":
        coach = {"head coach", "coach"}
        if ra & coach and not rb & coach:
            return f"{a} coached {b} at {title}"
        if rb & coach and not ra & coach:
            return f"{b} coached {a} at {title}"
        if ra & coach and rb & coach:
            return f"{a} and {b} were both on the coaching staff of {title}"
        return f"{a} and {b} were teammates at {title}"
    if kind == "election":
        return f"{a} and {b} were both candidates in {_the(title)}"
    if kind == "award":
        award = _the(ctx["name"])
        when = f" in {when.strip('()')}" if when else ""
        if "recipient" in ra and "recipient" in rb:
            return f"{a} and {b} both received {award}{when}"
        if "recipient" in ra:
            return f"{a} won and {b} was nominated for {award}{when}"
        if "recipient" in rb:
            return f"{b} won and {a} was nominated for {award}{when}"
        return f"{a} and {b} were both nominated for {award}{when}"
    if kind == "event":
        return f"{a} and {b} both took part in {_the(title)}"
    if kind == "document":
        return f"{a} and {b} both signed {_the(title)}"
    if kind == "org":
        return f"{a} and {b} co-founded {title}"
    if kind == "spaceflight":
        return f"{a} and {b} flew together on {title}"
    return f"{a} and {b} are both connected to {title}"


_PERFORM = {"performer", "instrument", "vocal", "conductor", "chorus master", "concertmaster"}
_MUSIC_WRITING = {"composer", "lyricist", "writer", "librettist"}
_WRITING = _MUSIC_WRITING | {"author"}
# MusicBrainz credit names as they read in "(role)"
_ROLE_LABEL = {"vocal": "vocals", "instrument": "musician", "recording": "recording engineer",
               "mix": "mixing engineer", "programming": "programmer", "writer": "songwriter"}


def _role(roles: set) -> str:
    order = ["director", "cast member", "voice actor", "screenwriter", "producer", "performer", "composer",
             "lyricist", "author", "creator", "illustrator", "editor", "conductor", "vocal", "instrument", "writer",
             "member", "founder", "supporting musician", "collaborator"]
    for r in order:
        if r in roles:
            return _ROLE_LABEL.get(r, r)
    r = next(iter(sorted(x for x in roles if x)), "")
    return _ROLE_LABEL.get(r, r) or "contributor"


_DESCENDANT = re.compile(r"\b(child|children|son|daughter|grandchild|grandson|granddaughter|nephew|niece|descendant)\b", re.I)
_ANCESTOR = re.compile(r"\b(parent|father|mother|grandparent|grandfather|grandmother|uncle|aunt|ancestor)\b", re.I)


def _kinship_flipped(detail: str, src: dict, dst: dict) -> bool:
    """Wikidata sometimes gives a kinship term from the other person's side ("relative: Lord Byron,
    kinship: great-great-great-great-grandchild" on his descendant's entry). Birth years give it away."""
    bs, bd = src.get("birth_year"), dst.get("birth_year")
    if bs is None or bd is None or "in-law" in detail:
        return False
    if _DESCENDANT.search(detail):
        return bd < bs - 10
    if _ANCESTOR.search(detail):
        return bd > bs + 10
    return False


def relation_sentence(rel: dict, A: dict, B: dict) -> str:
    """A and B are the two people in path order; rel['a_id'] is the relation's 'from' side."""
    t, detail, year = rel["type"], rel.get("detail"), rel.get("year")
    src, dst = (A, B) if rel["a_id"] == A["id"] else (B, A)
    s, d = src["name"], dst["name"]
    if t == "spouse":
        return f"{A['name']} and {B['name']} " + (f"married in {year}" if year else "were married")
    if t == "partner":
        return f"{A['name']} and {B['name']} were partners"
    if t == "parent":
        if src is A:
            return f"{s} is the {_gendered(src.get('gender'), 'father', 'mother', 'parent')} of {d}"
        return f"{d} is the {_gendered(dst.get('gender'), 'son', 'daughter', 'child')} of {s}"
    if t == "sibling":
        return f"{A['name']} is the {_gendered(A.get('gender'), 'brother', 'sister', 'sibling')} of {B['name']}"
    if t == "relative":
        if detail:  # detail is how dst relates to src ("cousin", "uncle"...)
            return f"{s} is {d}'s {detail}" if _kinship_flipped(detail, src, dst) else f"{d} is {s}'s {detail}"
        return f"{A['name']} and {B['name']} are relatives"
    if t == "stepparent":
        return f"{s} is the {_gendered(src.get('gender'), 'stepfather', 'stepmother', 'stepparent')} of {d}"
    if t == "godparent":
        return f"{s} is the {_gendered(src.get('gender'), 'godfather', 'godmother', 'godparent')} of {d}"
    if t == "advisor":
        return f"{s} was the doctoral advisor of {d}"
    if t == "teacher":
        return f"{s} taught {d}"
    if t == "academic_advisor":
        return f"{s} was an academic advisor of {d}"
    if t == "coach":
        return f"{s} coached {d}"
    if t == "supporting":
        return f"{s} was a supporting musician for {d}" + (f" ({detail})" if detail else "")
    if t == "served_during":   # detail: the office dst held
        return f"{s} served while {d} was {detail}"
    if t == "deputy":
        return f"{s} served as {detail} to {d}"
    if t == "associated":
        return f"{A['name']} and {B['name']} are listed as associated acts"
    if t == "influence":
        return f"{d} was influenced by {s}"
    if t == "partner_sport":
        return f"{A['name']} and {B['name']} were partners in sport or business"
    if t == "significant":
        return f"{d} was a significant person for {s}" + (f" ({detail})" if detail else "")
    if t == "succession":
        office = f" as {detail}" if detail else ""
        return f"{d} succeeded {s}{office}" + (f" in {year}" if year else "")
    return f"{A['name']} and {B['name']} are connected"
