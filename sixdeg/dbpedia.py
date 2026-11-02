"""Links from Wikipedia infoboxes (spouse, children, predecessor, band members...) that Wikidata
lacks, via DBpedia (CC BY-SA), queried on QLever's DBpedia endpoint like the Wikidata data.

People are matched exactly, never by name: a DBpedia resource is an English Wikipedia article, and
Wikidata records which article belongs to which person. (DBpedia's own owl:sameAs links to Wikidata
are unreliable for people: some articles point to hundreds of items.) Bands are matched by sameAs,
keeping only bands that point to exactly one Wikidata item.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from . import sparql
from .fetch import raw_path

ENDPOINT = os.environ.get("SIXDEG_DBPEDIA_ENDPOINT", "https://qlever.dev/api/dbpedia")

_PREFIX = """PREFIX dbo: <http://dbpedia.org/ontology/>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
"""
_WD = 'owl:sameAs ?%s . FILTER(STRSTARTS(STR(?%s), "http://www.wikidata.org/entity/"))'


def _res(*names: str) -> str:
    return " ".join('(STRAFTER(STR(?%s), "resource/") AS ?%s_)' % (n, n) for n in names)


@dataclass(frozen=True)
class Link:
    prop: str      # DBpedia ontology property, a --prop--> b
    type: str      # relation type stored in the database
    swap: bool     # store as (b, a)
    detail: str = ""

    @property
    def name(self) -> str:
        return "dbp_" + self.prop

    @property
    def sparql(self) -> str:
        return _PREFIX + "SELECT %s WHERE { ?a dbo:%s ?b }" % (_res("a", "b"), self.prop)


# Stored direction is (from, to) as in wikidata.DIRECT: parent->child, advisor->student,
# predecessor->successor, coach->athlete; for the office links (a served while b was president)
# and (deputy -> leader).
LINKS = [
    Link("spouse", "spouse", False),
    Link("partner", "partner", False),
    Link("child", "parent", False),
    Link("parent", "parent", True),
    Link("relative", "relative", False),
    Link("relation", "relative", False),
    Link("doctoralAdvisor", "advisor", True),
    Link("doctoralStudent", "advisor", False),
    Link("academicAdvisor", "academic_advisor", True),
    Link("notableStudent", "teacher", False),
    Link("predecessor", "succession", True),
    Link("successor", "succession", False),
    Link("associatedMusicalArtist", "associated", False),
    Link("coach", "coach", True),
    Link("trainer", "coach", True),
    Link("formerPartner", "partner_sport", False),
    Link("currentPartner", "partner_sport", False),
    Link("influencedBy", "influence", True),
    Link("influenced", "influence", False),
    Link("president", "served_during", False, "president"),
    Link("primeMinister", "served_during", False, "prime minister"),
    Link("chancellor", "served_during", False, "chancellor"),
    Link("governor", "served_during", False, "governor"),
    Link("monarch", "served_during", False, "monarch"),
    Link("vicePresident", "deputy", True, "vice president"),
    Link("deputy", "deputy", True, "deputy"),
    Link("lieutenant", "deputy", True, "lieutenant"),
]

# band -> member, and each band's Wikidata id(s) where DBpedia has one
BANDS = [
    ("dbp_band_members", _PREFIX + "SELECT %s WHERE { { ?x dbo:bandMember ?h } UNION { ?x dbo:formerBandMember ?h } }"
     % _res("x", "h")),
    ("dbp_band_wikidata", _PREFIX + 'SELECT DISTINCT %s (STRAFTER(STR(?q), "entity/") AS ?q_) WHERE { '
     "{ ?x dbo:bandMember ?h } UNION { ?x dbo:formerBandMember ?h } ?x %s }" % (_res("x"), _WD % ("q", "q"))),
]


def queries():
    return [(link.name, link.sparql) for link in LINKS] + BANDS


def fetch(raw_dir: str, refresh: bool = False, log=print) -> None:
    t0 = time.time()
    failed = []
    for name, query in queries():
        dest = raw_path(raw_dir, name)
        if os.path.exists(dest) and not refresh:
            continue
        log(f"  [dbpedia] {name} ...")
        try:
            rows = sparql.run_paged(query, dest, 2_000_000, log=log, endpoint=ENDPOINT)
        except sparql.QueryError as e:
            log(f"    FAILED: {e}")
            failed.append(name)
            continue
        log(f"    {rows:,} rows")
    log(f"  DBpedia finished in {time.time() - t0:.0f}s")
    if failed:
        raise SystemExit("DBpedia queries failed: " + ", ".join(failed) + " (re-run to retry just these)")
