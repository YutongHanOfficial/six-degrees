# Six Degrees

A people database built from public data (Wikidata), with a "six degrees of separation" search:
pick any two people and it finds the shortest chain of real connections between them, and explains
each link in plain English.

**Try it: https://yutonghanofficial.github.io/six-degrees/** (runs entirely in your browser; the
first search downloads about 31 MB, after which it is cached)

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

The current build has **7.3 million people**, **3.9 million direct relationships** and
**1.9 million shared contexts** (11 million memberships). Typical searches take 10–200 ms.

## Quick start

Requirements: Python 3.9+ and numpy (`pip3 install numpy`). Nothing else.

```bash
python3 sixdeg.py update        # download from Wikidata (~10 min, 515 MB) and build (15-30 min)
python3 sixdeg.py serve --open  # web app at http://127.0.0.1:8765
```

The build needs about 6 GB of free disk while it runs (a scratch `stage.db`, deleted at the end
unless you pass `--keep-stage`; with it kept, `build --reuse-stage` skips re-loading the raw files).

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
GitHub Pages (or Cloudflare Pages / Netlify). Everything works there: all 7.3 million people, the
same connection search, link types, "Avoid", alternatives, person panels and shareable links.

```bash
python3 sixdeg.py site                       # export ./site from data/ (≈ 25 min)
python3 sixdeg.py serve-site --open          # try it at http://127.0.0.1:8766
python3 sixdeg.py deploy YOUR-NAME/six-degrees   # push ./site to that repo's gh-pages branch
```

Then turn on Pages for the repository (Settings → Pages → Deploy from a branch → `gh-pages`, `/`).
After a data `update`, run `site` and `deploy` again; `deploy` replaces the branch with a single
commit, so old data doesn't accumulate in the repository.

### Monthly refresh

`.github/workflows/refresh.yml` does all of that on GitHub's machines on the 3rd of every month
(free for public repositories): `update`, `site`, the browser-vs-Python tests below, a check that
the new data isn't more than 5% smaller than what is live (a sign that a download came back
short), then `deploy`. If any step fails the live site stays as it was and GitHub emails you. A run
takes about 40 minutes; start one by hand from the Actions tab ("Monthly data refresh" → "Run workflow"),
and turn it off there too ("…" → "Disable workflow").

How it works without a server:

- **The connection search runs in the visitor's browser.** The graph (who is linked to whom) is
  packed into ~31 MB of compressed files that a Web Worker downloads once — the browser caches them
  — and turns back into the same arrays `graph.py` uses; `sixdeg/web/worker.js` is a line-by-line
  port of the bidirectional search, scoring included. `tests/test_static.mjs` checks it against
  the Python version: same degrees, same people visited, same best-route score.
- **Everything else is read on demand.** Names, descriptions, photos, link details, film/team
  names and the name index are split into small gzip'd JSON shards, packed into ~20 MB files.
  The page fetches just the shards it needs with HTTP range requests (GitHub Pages, Cloudflare and
  Netlify all support them; `serve-site` does too, unlike `python3 -m http.server`).

Limits worth knowing: GitHub Pages allows 1 GB per site (this uses about 600 MB) and asks for
roughly 100 GB of traffic a month at most, which is about 3,000 first-time visitors (returning
visitors use their cached copy). For more traffic, put the same `site/` folder on Cloudflare Pages,
which has no bandwidth cap (every file stays under its 25 MiB limit). The search needs a recent
browser (2023 or later) and about 350 MB of memory, so very old phones may struggle.

## What counts as a connection

One degree is either a **direct relationship** or **having been part of the same small group**.

| Link type | From Wikidata | Example sentence |
|---|---|---|
| Family | spouse, partner, parent/child, sibling, relative (with kinship), step/godparent; family groups | "Malia Obama is the daughter of Barack Obama" |
| Film & TV | same film/series/episode as cast, director, writer, producer, editor, cinematographer, composer, presenter, guest | "Tom Hanks and Kevin Bacon both appeared in Apollo 13 (1995)" |
| Music | same band; performers/composers/lyricists of the same song or album | "John Lennon and Paul McCartney were bandmates in The Beatles" |
| Sports | same team in the same season (from dated team memberships), coach–player, doubles/pairs partners | "X and Y were teammates at Manchester United F.C. (2007–08)" |
| Politics & office | succeeded someone in an office or title; candidates in the same election; signatories of the same document | "Barack Obama succeeded George W. Bush as President of the United States in 2009" |
| Science & teaching | doctoral advisor/student, teacher/student, co-authors of a paper | "Niels Bohr was the doctoral advisor of …" |
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

Direct relationships get sanity checks too: a parent must be 12–75 years older than the child and
alive (give or take a year) at the birth; spouses, partners, teachers and advisors must have been
alive at the same time. About 2% of Wikidata's parent links fail this, almost all data-entry errors.

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
| `person` | id, Wikidata `qid`, name, description, birth/death year, gender, `sitelinks` (number of Wikipedia/Wikimedia pages — a fame proxy), Commons image, English Wikipedia title, link counts. `id` is ordered by prominence. |
| `relation` | direct links: `a_id`, `b_id`, `type`, `category`, `detail` (office held, kinship term…), `year` |
| `context` | films, team-seasons, bands, papers, elections, …: `kind`, `category`, `name`, `year`, `period`, `size` |
| `membership` | who was in which context, with their `role` |
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
- Photos are loaded on demand from Wikimedia Commons (each file has its own licence).
- It covers people who have a Wikidata entry: anyone notable enough for a Wikipedia/Wikimedia page,
  plus people recorded through those connections (film credits, genealogies, doctoral lineages).
  It does not scrape social networks or people-search sites.
- A missing link means Wikidata doesn't record it, not that it doesn't exist (Frida Kahlo and Leon
  Trotsky's affair isn't there, so they come out 4 degrees apart). Wrong links come straight from
  Wikidata too: at the time of this build it listed Serena Williams as the mother of Isabella Soames.
  Fix the item on Wikidata and the next `update` picks it up.
- Team-season overlap is inferred from start/end dates (year-only dates are read as football-style
  seasons), so two players at the same club in the same season count as teammates.

## Layout

```
sixdeg.py            command line (update / fetch / build / serve / connect / search / person / stats)
sixdeg/wikidata.py   the SPARQL queries
sixdeg/sparql.py     paged bulk-export client
sixdeg/fetch.py      download step
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
