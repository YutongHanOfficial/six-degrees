# Six Degrees

A people database built from public data (Wikidata, plus links from Wikipedia infoboxes,
MusicBrainz and dblp), with a "six degrees of separation" search:
pick any two people and it finds the shortest chain of real connections between them, and explains
each link in plain English.

**Try it: https://yutonghanofficial.github.io/six-degrees/** (runs entirely in your browser; the
first search downloads about 41 MB, after which it is cached)

```
$ python3 sixdeg.py connect "Elizabeth II" "Elvis Presley"

Elizabeth II → Elvis Presley
3 degrees of separation (searched 19,588 people in 62 ms)

  Elizabeth II  (1926–2022 · Queen of the United Kingdom from 1952 to 2022)  [Q9682]
    └─ Elizabeth II and Donald J. Trump both took part in the 2019 state visit by Donald
       Trump to the United Kingdom
  Donald J. Trump  (b. 1946 · President of the United States (2017–2021; since 2025))  [Q22686]
    └─ Donald J. Trump and Priscilla Presley both appeared in Spin City (1996)
  Priscilla Presley  (b. 1945 · American actress and businesswoman)  [Q234773]
    └─ Priscilla Presley and Elvis Presley married in 1967  (+2 more in common)
  Elvis Presley  (1935–1977 · American singer and actor)  [Q303]

Other routes of the same length:
  Elizabeth II  →  Daniel Craig  →  Riley Keough  →  Elvis Presley
```

The website has **7.4 million people**, **4.0 million direct relationships** and **3.0 million
shared contexts** (14.5 million memberships). A full local build also has the 4.6 million people
who are only in MusicBrainz or dblp: 12 million people, 8.2 million contexts and 36 million
memberships. Typical searches take 10–200 ms.

## Quick start

Requirements: Python 3.9+ and numpy (`pip3 install numpy`). Nothing else, but the build wants
about 6 GB of memory and 25 GB of free disk.

```bash
python3 sixdeg.py update        # download (~30 min, 3.3 GB) and build (~30 min)
python3 sixdeg.py serve --open  # web app at http://127.0.0.1:8765
```

`update` downloads Wikidata (515 MB, through QLever), Wikipedia infobox links (through QLever's
DBpedia endpoint) and the MusicBrainz and dblp dumps (2.8 GB), then builds. The build keeps a
scratch `stage.db` while it runs, deleted at the end unless you pass `--keep-stage`; with it kept,
`build --reuse-stage` skips re-loading the raw files. `build --wikidata-people-only` leaves out
the people who are only in MusicBrainz or dblp, as the website does.

Command line:

```bash
python3 sixdeg.py connect "Kevin Bacon" "Barack Obama"
python3 sixdeg.py connect "Taylor Swift" "Isaac Newton" --avoid "Elizabeth II"
python3 sixdeg.py connect "Marie Curie" "Albert Einstein" --only science,family
python3 sixdeg.py search "lovelace"
python3 sixdeg.py person "Ada Lovelace"
python3 sixdeg.py stats
```

`update` is resumable: downloads are cached in `data/raw/`, so re-running it only fetches what is
missing. `python3 sixdeg.py fetch --refresh` re-downloads everything (Wikidata changes daily);
`python3 sixdeg.py build` rebuilds the database from the cached files.

## Put it on the web for free (GitHub Pages)

The full app also works as a static website, with no server, so it can be hosted for free on
GitHub Pages (or Cloudflare Pages / Netlify). Everything works there: all 7.4 million people, the
same connection search, link types, "Avoid", alternatives, person panels and shareable links. Build
it with `build --wikidata-people-only`: with everyone from MusicBrainz and dblp too, the site would
be 1.4 GB (over GitHub Pages' 1 GB) and the in-browser search would need about 1 GB of memory.

```bash
python3 sixdeg.py site                       # export ./site from data/ (≈ 10 min)
python3 sixdeg.py serve-site --open          # try it at http://127.0.0.1:8766
python3 sixdeg.py deploy YOUR-NAME/six-degrees   # push ./site to that repo's gh-pages branch
```

Then turn on Pages for the repository (Settings → Pages → Deploy from a branch → `gh-pages`, `/`).
After a data `update`, run `site` and `deploy` again; `deploy` replaces the branch with a single
commit, so old data doesn't accumulate in the repository.

### Monthly refresh

`.github/workflows/refresh.yml` does all of that on GitHub's machines on the 3rd of every month
(free for public repositories): `fetch`, `build --wikidata-people-only`, `site`, the
browser-vs-Python tests below, a check that the new data isn't more than 5% smaller than what is
live (a sign that a download came back short), then `deploy`. If any step fails the live site stays
as it was and GitHub emails you. A run takes about 2 hours; start one by hand from the Actions tab
("Monthly data refresh" → "Run workflow"), and turn it off there too ("…" → "Disable workflow").

How it works without a server:

- **The connection search runs in the visitor's browser.** The graph (who is linked to whom) is
  packed into ~41 MB of compressed files that a Web Worker downloads once — the browser caches them
  — and turns back into the same arrays `graph.py` uses; `sixdeg/web/worker.js` is a line-by-line
  port of the bidirectional search, scoring included. `tests/test_static.mjs` checks it against
  the Python version: same degrees, same people visited, same best-route score.
- **Everything else is read on demand.** Names, descriptions, photos, link details, film/team
  names and the name index are split into small gzip'd JSON shards, packed into ~20 MB files.
  The page fetches just the shards it needs with HTTP range requests (GitHub Pages, Cloudflare and
  Netlify all support them; `serve-site` does too, unlike `python3 -m http.server`).

Limits worth knowing: GitHub Pages allows 1 GB per site (this uses about 700 MB) and asks for
roughly 100 GB of traffic a month at most, which is about 3,000 first-time visitors (returning
visitors use their cached copy). For more traffic, put the same `site/` folder on Cloudflare Pages,
which has no bandwidth cap (every file stays under its 25 MiB limit). The search needs a recent
browser (2023 or later) and about 400 MB of memory, so very old phones may struggle.

## What counts as a connection

One degree is either a **direct relationship** or **having been part of the same small group**.

| Link type | What counts | Example sentence |
|---|---|---|
| Family | spouse, partner, parent/child, sibling, relative (with kinship), step/godparent; family groups | "Malia Obama is the daughter of Barack Obama" |
| Film & TV | same film/series/episode as cast, director, writer, producer, editor, cinematographer, composer, presenter, guest | "Tom Hanks and Kevin Bacon both appeared in Apollo 13 (1995)" |
| Music | same band; performed on, produced or wrote the same recording, song or album; supporting musicians; "associated acts" | "John Lennon and Paul McCartney were bandmates in The Beatles" |
| Sports | same team in the same season (from dated team memberships), coach–player, doubles/pairs partners | "X and Y were teammates at Manchester United F.C. (2007–08)" |
| Politics & office | succeeded someone in an office or title; served under or as deputy to someone; candidates in the same election; signatories of the same document | "Barack Obama succeeded George W. Bush as President of the United States in 2009" |
| Science & teaching | doctoral or academic advisor/student, teacher/student, co-authors of a paper (with dblp for computer science) | "Niels Bohr was the doctoral advisor of …" |
| Business | co-founders of the same organisation | "Steve Jobs and Steve Wozniak co-founded Apple Inc." |
| Books & art | co-authors, illustrators, editors and co-creators of the same work | |
| Other | crewmates on a spaceflight, participants in the same small event (summit, final, battle), "significant person" statements | "Neil Armstrong and Buzz Aldrin flew together on Apollo 11" |
| Awards *(off by default)* | received or were nominated for the same award in the same year (≤ 20 people) | |
| Influence *(off by default)* | "influenced by" — usually people who never met | |

Shared contexts only count when they plausibly mean the two people worked together:

- **Small enough to mean something**: a film cast of up to 250, a team-season of up to 80, a band or
  club of up to 60, co-authors of up to 25, elections with up to 15 candidates, and so on
  (`CAPS` in `sixdeg/build.py`).
- **Alive at the time** (with two years' grace for posthumous releases): otherwise Hitler "appears
  in" *Jojo Rabbit* through archive footage, Shakespeare "works on" a Schubert song, and Leibniz
  "co-authors" an 1892 edition of his own book.
- **No documentary interviewees or archive footage**: the on-screen people in a documentary did not
  appear *together*; its crew still counts.
- **Events must be dated**: an undated "commencement at Harvard" would link speakers decades apart.
- **Nobody worked with someone born after they died**: in undated credits this drops the
  source-material member (Bach "working on" a 1994 ballet, Aesop on a 15th-century translation).
- **One per group of people**: MusicBrainz lists every recording, song and album, and dblp every
  paper, so the same few people can share hundreds. When several link exactly the same people, one
  is kept (Wikidata's own item first, then a song, a recording, an album; the earliest). Routes
  are unchanged by this, and the site is 2.5 million contexts smaller.

Direct relationships get sanity checks too: a parent must be 12–75 years older than the child and
alive (give or take a year) at the birth; spouses, partners, teachers and advisors must have been
alive at the same time. About 2% of Wikidata's parent links fail this, almost all data-entry errors.
Outside Wikidata, a marriage needs two statements (both people's Wikipedia infoboxes, or Wikipedia
and MusicBrainz): infobox spouse fields also link the spouse's parents and former spouses, and
MusicBrainz sometimes says "married" of long-term couples, so a marriage only MusicBrainz records
counts as a partnership. MusicBrainz's step-parents and step-siblings stay step relations.

## How the search works

The graph has people and "contexts" (a film, a team in one season, a band, an election…). Going
person → context → person costs one degree, the same as a direct relationship, which keeps the
graph compact (a 40-person cast is 40 links, not 780).

Search is a bidirectional breadth-first search written with numpy array operations: it grows a
frontier from both people, always expanding the cheaper side, and stops at the first level where
they meet. That guarantees the minimum number of degrees. Among all routes of that minimum length
it picks the one through the best-known people (scored by how many Wikipedia language editions
cover them) and the most recognisable links, and lists other equally short routes as alternatives.
"Avoid" re-runs the search with a person excluded.

There is no limit on the number of degrees. Each person is visited at most once from each side,
so a search ends after at most one pass over the graph: either the two sides meet, or one of them
runs out of people, which proves there is no connection at all ("X's whole network is 70 other
people, and Y isn't one of them"). Two well-known people are typically about 5 degrees apart and
two random people about 9, but about a quarter of connected random pairs are more than 12 apart,
and long family trees make some much further: Ramesses III → Philip Sidney is 58 degrees, through Assyrian kings,
Hillel, Paul the Apostle and Mary I. On the website a long search shows its progress and has a
Stop button; `connect --max N` gives up beyond N degrees.

## The database (`data/people.db`)

Plain SQLite, so you can query it directly:

| Table | What |
|---|---|
| `person` | id, `qid` (the Wikidata id, or `mb:…` / `dblp:…` for someone only in MusicBrainz or dblp), name, description, birth/death year, gender, `sitelinks` (number of Wikipedia/Wikimedia pages — a fame proxy), Commons image, English Wikipedia title, link counts. `id` is ordered by prominence. |
| `relation` | direct links: `a_id`, `b_id`, `type`, `category`, `detail` (office held, kinship term…), `year`, `source` |
| `context` | films, team-seasons, bands, papers, elections, …: `kind`, `category`, `name`, `year`, `period`, `size` |
| `membership` | who was in which context, with their `role` and the `source` that says so |
| `alias` | other names people go by (for search) |
| `person_search` | full-text name index (FTS5) |

```sql
-- everyone Ada Lovelace is directly related to
SELECT p.name, r.type, r.detail FROM relation r JOIN person p ON p.id = r.b_id
WHERE r.a_id = (SELECT id FROM person WHERE qid = 'Q7259')
UNION ALL
SELECT p.name, r.type, r.detail FROM relation r JOIN person p ON p.id = r.a_id
WHERE r.b_id = (SELECT id FROM person WHERE qid = 'Q7259');

-- the casts of the 10 best-known films Tom Hanks is in
SELECT c.name, c.year, c.size FROM membership m JOIN context c ON c.id = m.context_id
WHERE m.person_id = (SELECT id FROM person WHERE qid = 'Q2263') AND c.category = 'film'
ORDER BY c.sitelinks DESC LIMIT 10;
```

`data/graph/*.npy` holds the same links as compressed adjacency arrays for the search.

## Sources and scope

- **Wikidata** (CC0), fetched in bulk through the public [QLever](https://qlever.dev) SPARQL
  endpoint, which allows 30 s per request — big results are fetched in pages, and context names in
  batched lookups cached in `data/raw/entities.db`.
- **Wikipedia infoboxes** (CC BY-SA), via [DBpedia](https://www.dbpedia.org) on QLever's DBpedia
  endpoint: spouses, children, relatives, advisors, coaches, predecessors and successors, "served
  under" and deputy links, band members and "associated acts" that Wikidata lacks.
- **[MusicBrainz](https://musicbrainz.org)** (CC0), from its monthly JSON dump of artists (2 GB):
  band members, family ties, teachers, supporting musicians, and who performed on, produced or
  wrote which recordings, songs and albums.
- **[dblp](https://dblp.org)** (CC0), from its monthly XML dump (1.1 GB): co-authors of computer
  science papers.

Every link shows the sources that record it. People are matched across sources only through ids,
never by name, and only one-to-one: a Wikipedia article is tied to its person by Wikidata, and
MusicBrainz and dblp records by Wikidata's ids for them (P434, P2456) or their own links to
Wikidata. A record whose ids point to two different people stays separate, so the worst a
matching gap can do is miss a connection, not invent one.
- Photos are loaded on demand from Wikimedia Commons (each file has its own licence).
- It covers people who have a Wikidata entry: anyone notable enough for a Wikipedia/Wikimedia page,
  plus people recorded through those connections (film credits, genealogies, doctoral lineages).
  A full local build also adds everyone else in MusicBrainz and dblp (musicians, producers and
  computer scientists); the website leaves them out to stay within GitHub Pages' 1 GB (`build
  --wikidata-people-only`). It does not use social networks, people-search sites or public records
  about private individuals.
- A missing link means none of these sources records it, not that it doesn't exist (Frida Kahlo and
  Leon Trotsky's affair isn't there, so they come out 4 degrees apart). Wrong links come straight
  from the sources too: at the time of one build Wikidata listed Serena Williams as the mother of
  Isabella Soames. Fix the record at its source (each link names it) and the next `update` picks it up.
- Team-season overlap is inferred from start/end dates (year-only dates are read as football-style
  seasons), so two players at the same club in the same season count as teammates.

## Layout

```
sixdeg.py            command line (update / fetch / build / serve / connect / search / person / stats)
sixdeg/wikidata.py   the SPARQL queries
sixdeg/dbpedia.py    Wikipedia infobox links (queries on QLever's DBpedia endpoint)
sixdeg/musicbrainz.py, dblp.py   download and read those dumps
sixdeg/sparql.py     paged bulk-export client
sixdeg/fetch.py      download step
sixdeg/sources.py    matches the other sources to Wikidata and stages them
sixdeg/build.py      raw CSVs → people.db + graph arrays
sixdeg/graph.py      bidirectional search
sixdeg/store.py      name search, person pages, link sentences
sixdeg/server.py     JSON API + static web app (sixdeg/web/)
sixdeg/static_site.py   people.db + graph -> static website (site/)
sixdeg/static_server.py local preview of site/ with HTTP range requests
sixdeg/web/engine.js    the website's in-browser stand-in for the JSON API
sixdeg/web/worker.js    the website's connection search (port of graph.py)
tests/test_graph.py  search vs. brute-force BFS on random graphs
tests/test_static.mjs   browser code vs. the Python app (with tests/static_reference.py)
tests/check_refresh.py  refuses to publish a refresh that lost a lot of data
.github/workflows/refresh.yml   the monthly refresh
```
