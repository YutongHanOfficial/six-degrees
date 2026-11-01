"""Reference answers from the Python app, for tests/test_static.mjs.

  python3 tests/static_reference.py refs.json [n]     # n x 3.5 random graph searches
  python3 tests/static_reference.py --app app.json    # name search, link sentences, person panels
"""
import json
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sixdeg import CATEGORIES, DEFAULT_OFF  # noqa: E402
from sixdeg.graph import DIRECT_BONUS, Graph  # noqa: E402

KEYS = [k for k, _ in CATEGORIES]
DEFAULT = sorted(set(KEYS) - DEFAULT_OFF)


def path_score(g, people, links, mask):
    """Sum of person scores plus link scores along a route (as the search scores it)."""
    s = float(sum(g.p_score[p] for p in people))
    for i, via in enumerate(links):
        if via >= 0:
            s += float(g.c_score[via])
        else:
            a, b = people[i], people[i + 1]
            row = slice(g.d_ptr[a], g.d_ptr[a + 1])
            s += max(float(DIRECT_BONUS[c]) for n, c in zip(g.d_nbr[row], g.d_cat[row]) if n == b and mask[c])
    return s


QUERIES = [
    "kevin bacon", "bacon", "obama", "barack", "einstein", "marie curie", "curie", "taylor swift", "tay", "jo",
    "john smith", "beyonce", "beyoncé", "messi", "lionel m", "elizabeth ii", "napoleon", "q76", "da vinci",
    "leonardo da", "van gogh", "ada lovelace", "lovelace", "turing", "frida", "trotsky", "genghis", "bill gates",
    "zoë", "毛泽东", "s", "abraham lincoln", "lincoln", "winston churchill", "serena", "williams", "j r r",
    "tolkien", "lady gaga", "gaga", "elvis", "presley", "kim kardashian", "mozart", "bach", "shakespeare",
    "plato", "cleopatra", "confucius", "nelson mandela", "mandela", "pelé", "pele", "al", "mar", "smith j",
]
PAIRS = [("Kevin Bacon", "Barack Obama"), ("Albert Einstein", "Marie Curie"), ("Elizabeth II", "Elvis Presley"),
         ("Lionel Messi", "Cristiano Ronaldo"), ("Ada Lovelace", "Alan Turing"), ("Napoleon", "Beyoncé"),
         ("Genghis Khan", "Bill Gates"), ("Frida Kahlo", "Leon Trotsky"), ("Serena Williams", "Winston Churchill"),
         ("Taylor Swift", "Isaac Newton"), ("Tom Hanks", "Kevin Bacon"), ("Paul McCartney", "John Lennon")]
PANELS = ["Q7259", "Q76", "Q615", "Q2263", "Q9682", "Q303", "Q1035"]


def app_refs(out_path):
    from sixdeg.server import App
    app = App(os.path.join(os.path.dirname(__file__), "..", "data"))
    out = {"search": [{"q": q, "ids": [p["id"] for p in app.store.search(q, 8)]} for q in QUERIES],
           "connect": [], "person": []}
    for a, b in PAIRS:
        qa, qb = app.store.resolve(a)["qid"], app.store.resolve(b)["qid"]
        r = app.connect(qa, qb, None, "", k=4)
        out["connect"].append({"from": qa, "to": qb, "degrees": r["degrees"],
                               "sentences": [x["sentence"] for x in r["paths"][0]["links"]] if r["paths"] else []})
    for qid in PANELS:
        d = app.store.neighborhood(app.store.person_by_qid(qid)["id"])
        out["person"].append({"qid": qid, "summary": {
            "rel": {k: [len(v), [x["sentence"] for x in v[:25]]] for k, v in d["relations"].items()},
            "ctx": {k: [v["total"], [[x["title"], x["role"], x["size"], x["seasons"], [m["qid"] for m in x["with"]]]
                                     for x in v["items"]]] for k, v in d["contexts"].items()}}})
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"app references written to {out_path}")


def main():
    if sys.argv[1] == "--app":
        return app_refs(sys.argv[2])
    out_path, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 100
    g = Graph(os.path.join(os.path.dirname(__file__), "..", "data", "graph"))
    linked = np.flatnonzero((np.diff(g.d_ptr) + np.diff(g.pc_ptr)) > 0)
    rng = random.Random(7)
    cases = []
    for _ in range(n):
        cases.append((rng.randrange(20000), rng.randrange(20000), DEFAULT))           # well known
        cases.append((int(rng.choice(linked)), int(rng.choice(linked)), DEFAULT))     # anyone
        cases.append((rng.randrange(3000), int(rng.choice(linked)), DEFAULT))         # mixed
    for _ in range(n // 2):
        cases.append((rng.randrange(50000), rng.randrange(50000), sorted(rng.sample(KEYS, rng.randint(1, 6)))))
    refs = []
    for src, dst, cats in cases:
        mask = np.array([k in cats for k in KEYS])
        r = g.connect(src, dst, mask, (), k=3)
        refs.append(_ref(g, src, dst, cats, [], mask, r))
        if r["degrees"] and r["degrees"] >= 2 and rng.random() < 0.3:   # and again, avoiding someone
            avoid = [r["paths"][0][0][1]]
            refs.append(_ref(g, src, dst, cats, avoid, mask, g.connect(src, dst, mask, avoid, k=3)))
    with open(out_path, "w") as f:
        json.dump(refs, f)
    print(f"{len(refs)} reference searches written to {out_path}")


def _ref(g, src, dst, cats, avoid, mask, r):
    best = r["paths"][0] if r["paths"] else None
    return {"src": src, "dst": dst, "cats": cats, "avoid": avoid, "degrees": r["degrees"], "visited": r["visited"],
            "score": path_score(g, best[0], best[1], mask) if best else None, "path": best[0] if best else None,
            "ran_out": r.get("ran_out"), "reached": r.get("reached")}


if __name__ == "__main__":
    main()
