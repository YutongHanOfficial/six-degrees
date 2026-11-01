"""What the database pulls from Wikidata, expressed as SPARQL.

  * DIRECT   person -> person statements (spouse, parent, doctoral advisor, successor in office...)
  * MEMBERS  person in a shared context (cast of a film, squad of a team-season, band, election...)
  * PEOPLE   names / descriptions / years / notability for every human, fetched in pages
  * entity_info() / any_labels(): names for the contexts that survive filtering, in VALUES batches
Every query returns compact Q-ids: the "http://www.wikidata.org/entity/" prefix is stripped server-side.
The public QLever endpoint allows 30 s per request, so big results are paged (LIMIT/OFFSET in the
engine's natural, subject-sorted order) and nothing here relies on an expensive global union.
"""
from __future__ import annotations

from dataclasses import dataclass
from string import Template


def sel(*names: str) -> str:
    return " ".join('(STRAFTER(STR(?%s), "entity/") AS ?%s_)' % (n, n) for n in names)


def T(text: str, **kw: str) -> str:
    return Template(text).substitute(**kw)


@dataclass(frozen=True)
class Query:
    name: str
    sparql: str
    page: int = 2_500_000  # rows per request


# ---------------------------------------------------------------- direct relations

@dataclass(frozen=True)
class Direct:
    name: str    # raw file name
    type: str    # relation type stored in the database
    swap: bool   # query yields (to, from) rather than (from, to)
    sparql: str  # columns: a_, b_ [, detail_] [, year]


def _simple(prop: str) -> str:
    return T("SELECT $S WHERE { ?a wdt:$P ?b . ?a wdt:P31 wd:Q5 . ?b wdt:P31 wd:Q5 }",
             S=sel("a", "b"), P=prop)


def _qualified(prop: str, qualifier: str) -> str:
    return T("""SELECT $S WHERE {
  ?a p:$P ?st . ?st ps:$P ?b . ?a wdt:P31 wd:Q5 . ?b wdt:P31 wd:Q5 .
  OPTIONAL { ?st pq:$Q ?detail } }""", S=sel("a", "b", "detail"), P=prop, Q=qualifier)


def _succession(qualifier: str, date: str) -> str:
    # ?a held office/title ?detail and replaced (P1365) / was replaced by (P1366) ?b
    return T("""SELECT $S (YEAR(?when) AS ?year) WHERE {
  ?a p:P39|p:P97 ?st . ?st ps:P39|ps:P97 ?detail ; pq:$Q ?b .
  ?a wdt:P31 wd:Q5 . ?b wdt:P31 wd:Q5 .
  OPTIONAL { ?st pq:$D ?when } }""", S=sel("a", "b", "detail"), Q=qualifier, D=date)


# Stored direction is (from, to): parent->child, advisor->student, teacher->student,
# influencer->influenced, predecessor->successor, (subject, related person) otherwise.
DIRECT = [
    Direct("rel_spouse", "spouse", False, T("""SELECT $S (YEAR(?start) AS ?year) WHERE {
  ?a p:P26 ?st . ?st ps:P26 ?b . ?a wdt:P31 wd:Q5 . ?b wdt:P31 wd:Q5 .
  OPTIONAL { ?st pq:P580 ?start } }""", S=sel("a", "b"))),
    Direct("rel_partner", "partner", False, _simple("P451")),
    Direct("rel_child", "parent", False, _simple("P40")),
    Direct("rel_father", "parent", True, _simple("P22")),
    Direct("rel_mother", "parent", True, _simple("P25")),
    Direct("rel_sibling", "sibling", False, _simple("P3373")),
    Direct("rel_relative", "relative", False, _qualified("P1038", "P1039")),   # detail = kinship
    Direct("rel_stepparent", "stepparent", True, _simple("P3448")),
    Direct("rel_godparent", "godparent", True, _simple("P1290")),
    Direct("rel_doctoral_advisor", "advisor", True, _simple("P184")),
    Direct("rel_doctoral_student", "advisor", False, _simple("P185")),
    Direct("rel_student_of", "teacher", True, _simple("P1066")),
    Direct("rel_student", "teacher", False, _simple("P802")),
    Direct("rel_influenced_by", "influence", True, _simple("P737")),
    Direct("rel_sport_business_partner", "partner_sport", False, _simple("P1327")),
    Direct("rel_significant_person", "significant", False, _qualified("P3342", "P3831")),  # detail = role
    Direct("rel_replaces", "succession", True, _succession("P1365", "P580")),
    Direct("rel_replaced_by", "succession", False, _succession("P1366", "P582")),
]

# ---------------------------------------------------------------- shared contexts

@dataclass(frozen=True)
class Members:
    name: str     # raw file name
    family: str   # context family (see build.CAPS)
    role: str
    pattern: str  # binds ?x (the context) and ?h (the person)
    extra: str = ""  # additional SELECT columns
    page: int = 2_500_000

    @property
    def sparql(self) -> str:
        return "SELECT %s %s WHERE { %s }" % (sel("x", "h"), self.extra, self.pattern)


def _obj(prop: str) -> str:   # context --prop--> person
    return "?x wdt:%s ?h . ?h wdt:P31 wd:Q5 ." % prop


def _subj(prop: str) -> str:  # person --prop--> context
    return "?h wdt:%s ?x . ?h wdt:P31 wd:Q5 ." % prop


WORK_ROLES = [
    ("P161", "cast member"), ("P57", "director"), ("P58", "screenwriter"), ("P162", "producer"),
    ("P1431", "executive producer"), ("P344", "cinematographer"), ("P1040", "film editor"),
    ("P725", "voice actor"), ("P371", "presenter"), ("P5030", "talk show guest"),
    ("P3092", "film crew"), ("P86", "composer"), ("P175", "performer"), ("P676", "lyricist"),
    ("P87", "librettist"), ("P110", "illustrator"), ("P98", "editor"),
]
FILM_ROLES = {"cast member", "director", "screenwriter", "producer", "executive producer",
              "cinematographer", "film editor", "voice actor", "presenter", "talk show guest", "film crew"}
MUSIC_ROLES = {"composer", "performer", "lyricist", "librettist"}

# Works with 2+ creators (single-artist paintings are useless as links).
_MULTI_CREATOR = """{ SELECT ?x WHERE { ?x wdt:P170 ?c . ?c wdt:P31 wd:Q5 } GROUP BY ?x HAVING (COUNT(?c) >= 2) }
?x wdt:P170 ?h . ?h wdt:P31 wd:Q5 ."""

# Co-authorship (books and scholarly papers) among authors who have a Wikipedia/Wikimedia page.
# Grouping happens locally: server-side GROUP BY over all 36M author statements exceeds the time limit.
_AUTHORED = "?x wdt:P50 ?h . ?h wdt:P31 wd:Q5 ; wikibase:sitelinks ?s FILTER(?s > 0)"


def _dated(stmt: str) -> str:
    return stmt + " ?h wdt:P31 wd:Q5 . OPTIONAL { ?st pq:P580 ?s } OPTIONAL { ?st pq:P582 ?e }"


_DATES = '(STR(?s) AS ?start) (STR(?e) AS ?end)'

# Cast entries that are only archive footage (Hitler "appearing" in Jojo Rabbit) are not connections.
_CAST = """?x p:P161 ?st . ?st ps:P161 ?h . ?h wdt:P31 wd:Q5 .
MINUS { ?st pq:P1552 wd:Q101437313 }
MINUS { ?st pq:P4633 ?role FILTER(CONTAINS(LCASE(STR(?role)), "archive")) }"""

MEMBERS = (
    [Members("mem_work_%s" % p, "work", role, _CAST if p == "P161" else _obj(p)) for p, role in WORK_ROLES]
    + [
        Members("mem_work_P170", "work", "creator", _MULTI_CREATOR),
        Members("mem_authored_P50", "authored", "author", _AUTHORED, page=1_500_000),
        Members("mem_group_P463", "group", "member", _subj("P463")),
        Members("mem_group_P527", "group", "member", _obj("P527")),
        Members("mem_team_P54", "team", "player", _dated("?h p:P54 ?st . ?st ps:P54 ?x ."), _DATES, 1_000_000),
        Members("mem_team_P286", "team", "head coach", _dated("?x p:P286 ?st . ?st ps:P286 ?h ."), _DATES),
        Members("mem_team_P6087", "team", "coach", _dated("?h p:P6087 ?st . ?st ps:P6087 ?x ."), _DATES),
        Members("mem_election_P726", "election", "candidate", _obj("P726")),
        Members("mem_election_P3602", "election", "candidate", _subj("P3602")),
        Members("mem_award_P166", "award", "recipient",
                "?h p:P166 ?st . ?st ps:P166 ?x ; pq:P585 ?d . ?h wdt:P31 wd:Q5 .", "(YEAR(?d) AS ?year)"),
        Members("mem_award_P1411", "award", "nominee",
                "?h p:P1411 ?st . ?st ps:P1411 ?x ; pq:P585 ?d . ?h wdt:P31 wd:Q5 .", "(YEAR(?d) AS ?year)"),
        Members("mem_event_P710", "event", "participant", _obj("P710")),
        Members("mem_event_P1344", "event", "participant", _subj("P1344")),
        Members("mem_document_P1891", "document", "signatory", _obj("P1891")),
        Members("mem_org_P112", "org", "founder", _obj("P112")),
        Members("mem_spaceflight_P1029", "spaceflight", "crew member", _obj("P1029")),
    ]
)

FLAGS = {
    # groups that are musical ensembles -> "music"; families/dynasties -> "family"
    "flag_music_groups": T("""SELECT DISTINCT $X WHERE { { ?h wdt:P463 ?x } UNION { ?x wdt:P527 ?h }
  ?h wdt:P31 wd:Q5 . ?x wdt:P31/wdt:P279* wd:Q2088357 }""", X=sel("x")),
    "flag_family_groups": T("""SELECT DISTINCT $X WHERE { { ?h wdt:P463 ?x } UNION { ?x wdt:P527 ?h }
  ?h wdt:P31 wd:Q5 . ?x wdt:P31/wdt:P279* wd:Q8436 }""", X=sel("x")),
    # documentaries: their on-screen "cast" are interviewees and archive clips, not people working together
    "flag_documentaries": T("""SELECT DISTINCT $X WHERE { ?x wdt:P161 ?h . ?h wdt:P31 wd:Q5 .
  { ?x wdt:P31/wdt:P279* wd:Q93204 } UNION { ?x wdt:P136/wdt:P279* wd:Q93204 } }""", X=sel("x")),
}

# ---------------------------------------------------------------- people (all humans, paged)

_H = sel("h")
PEOPLE = [
    Query("people_label_en", T("""SELECT $H ?name WHERE { ?h wdt:P31 wd:Q5 .
  ?h rdfs:label ?name FILTER(LANG(?name) = "en") }""", H=_H), 1_200_000),
    Query("people_label_mul", T("""SELECT $H ?name WHERE { ?h wdt:P31 wd:Q5 .
  ?h rdfs:label ?name FILTER(LANG(?name) = "mul") MINUS { ?h rdfs:label ?en FILTER(LANG(?en) = "en") } }""",
                                H=_H), 1_200_000),
    Query("people_label_any", T("""SELECT $H (SAMPLE(?l) AS ?name) WHERE { ?h wdt:P31 wd:Q5 .
  MINUS { ?h rdfs:label ?e FILTER(LANG(?e) = "en" || LANG(?e) = "mul") } ?h rdfs:label ?l } GROUP BY ?h""",
                                H=_H), 1_000_000),
    Query("people_desc", T("""SELECT $H ?desc WHERE { ?h wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl FILTER(?sl > 0)
  ?h schema:description ?desc FILTER(LANG(?desc) = "en") }""", H=_H), 1_000_000),
    Query("people_birth", T("""SELECT $H (YEAR(?d) AS ?year) WHERE { ?h wdt:P31 wd:Q5 ; wdt:P569 ?d }""", H=_H),
          3_000_000),
    Query("people_death", T("""SELECT $H (YEAR(?d) AS ?year) WHERE { ?h wdt:P31 wd:Q5 ; wdt:P570 ?d }""", H=_H),
          3_000_000),
    Query("people_gender", T("""SELECT $H (STRAFTER(STR(?g), "entity/") AS ?gender) WHERE {
  ?h wdt:P31 wd:Q5 ; wdt:P21 ?g }""", H=_H), 3_000_000),
    Query("people_sitelinks", T("""SELECT $H ?sl WHERE { ?h wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl FILTER(?sl > 0) }""",
                                H=_H), 3_000_000),
    Query("people_image", T("""SELECT $H (STRAFTER(STR(?i), "FilePath/") AS ?image) WHERE {
  ?h wdt:P31 wd:Q5 ; wdt:P18 ?i }""", H=_H), 1_000_000),
    Query("people_enwiki", T("""SELECT $H ?title WHERE { ?a schema:about ?h ;
  schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title . ?h wdt:P31 wd:Q5 }""", H=_H), 1_000_000),
    Query("people_alias", T("""SELECT $H ?alias WHERE { ?h wdt:P31 wd:Q5 ; wikibase:sitelinks ?sl FILTER(?sl >= 2)
  ?h skos:altLabel ?alias FILTER(LANG(?alias) = "en") }""", H=_H), 1_000_000),
]

# ---------------------------------------------------------------- entity lookups (VALUES batches)


def entity_info(qids) -> str:
    """Name (English, else multilingual), earliest date, sitelink count and whether the item is a
    scholarly article, for a batch of Q-ids."""
    return T("""SELECT $X (SAMPLE(?nm) AS ?name) (MIN(?yr) AS ?year) (MAX(?sl0) AS ?sl) (MAX(?sch0) AS ?scholarly)
WHERE {
  VALUES ?x { $V }
  OPTIONAL { ?x rdfs:label ?en FILTER(LANG(?en) = "en") }
  OPTIONAL { ?x rdfs:label ?mul FILTER(LANG(?mul) = "mul") }
  BIND(COALESCE(?en, ?mul) AS ?nm)
  OPTIONAL { ?x wikibase:sitelinks ?sl0 }
  OPTIONAL { ?x wdt:P577|wdt:P571|wdt:P580|wdt:P585|wdt:P619 ?dt . BIND(YEAR(?dt) AS ?yr) }
  OPTIONAL { ?x wdt:P31 wd:Q13442814 . BIND(1 AS ?sch0) }
} GROUP BY ?x""", X=sel("x"), V=" ".join("wd:" + q for q in qids))


def any_labels(qids) -> str:
    """A label in any language, for items with no English one."""
    return T("""SELECT $X (SAMPLE(?l) AS ?name) WHERE { VALUES ?x { $V } ?x rdfs:label ?l } GROUP BY ?x""",
             X=sel("x"), V=" ".join("wd:" + q for q in qids))


def bulk_queries():
    """Every query run by the fetch step."""
    out = [Query(d.name, d.sparql) for d in DIRECT]
    out += [Query(m.name, m.sparql, m.page) for m in MEMBERS]
    out += [Query(n, q) for n, q in FLAGS.items()]
    return out + PEOPLE
