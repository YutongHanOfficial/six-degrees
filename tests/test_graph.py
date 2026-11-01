"""Checks the bidirectional search against a brute-force BFS on random graphs.

Run: python3 tests/test_graph.py
"""
import json
import os
import random
import sys
import tempfile
from collections import deque

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sixdeg import CATEGORIES  # noqa: E402
from sixdeg.build import _csr  # noqa: E402
from sixdeg.graph import Graph  # noqa: E402

NCAT = len(CATEGORIES)


def make_graph(rng, P, C, n_direct, n_member):
    direct = set()
    while len(direct) < n_direct:
        a, b = rng.randrange(P), rng.randrange(P)
        if a != b:
            direct.add((min(a, b), max(a, b), rng.randrange(NCAT)))
    member = set()
    while len(member) < n_member:
        member.add((rng.randrange(P), rng.randrange(C)))
    c_cat = [rng.randrange(NCAT) for _ in range(C)]
    return sorted(direct), sorted(member), c_cat


def write(d, P, C, direct, member, c_cat, rng):
    rel = np.array(direct, np.int64).reshape(-1, 3)
    src = np.concatenate([rel[:, 0], rel[:, 1]])
    dst = np.concatenate([rel[:, 1], rel[:, 0]])
    cat = np.concatenate([rel[:, 2], rel[:, 2]])
    d_ptr, d_nbr, order = _csr(src, dst, P)
    mem = np.array(member, np.int64).reshape(-1, 2)
    pc_ptr, pc_ctx, _ = _csr(mem[:, 0], mem[:, 1], P)
    cp_ptr, cp_per, _ = _csr(mem[:, 1], mem[:, 0], C)
    arrays = dict(p_score=np.array([rng.random() * 5 for _ in range(P)], np.float32),
                  c_cat=np.array(c_cat, np.uint8), c_score=np.array([rng.random() for _ in range(C)], np.float32),
                  d_ptr=d_ptr, d_nbr=d_nbr, d_cat=cat[order].astype(np.uint8),
                  pc_ptr=pc_ptr, pc_ctx=pc_ctx, cp_ptr=cp_ptr, cp_per=cp_per)
    for k, v in arrays.items():
        np.save(os.path.join(d, k + ".npy"), v)
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump({"persons": P, "contexts": C, "direct_edges": len(direct), "memberships": len(member),
                   "categories": [k for k, _ in CATEGORIES]}, f)


def reference(P, direct, member, c_cat, allowed, src, avoid):
    adj = [[] for _ in range(P)]
    for a, b, c in direct:
        if allowed[c]:
            adj[a].append(b)
            adj[b].append(a)
    by_ctx = {}
    for p, c in member:
        if allowed[c_cat[c]]:
            by_ctx.setdefault(c, []).append(p)
    for people in by_ctx.values():
        for a in people:
            adj[a].extend(x for x in people if x != a)
    dist = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v not in dist and v not in avoid:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist, adj


def main():
    rng = random.Random(7)
    checked = 0
    for trial in range(60):
        P = rng.randrange(20, 400)
        C = rng.randrange(5, 120)
        direct, member, c_cat = make_graph(rng, P, C, rng.randrange(0, P), rng.randrange(0, P * 2))
        with tempfile.TemporaryDirectory() as d:
            write(d, P, C, direct, member, c_cat, rng)
            g = Graph(d)
            for _ in range(25):
                allowed = np.array([rng.random() < 0.8 for _ in range(NCAT)], bool)
                src, dst = rng.randrange(P), rng.randrange(P)
                avoid = set(rng.sample(range(P), rng.randrange(0, 3))) - {src, dst}
                ref, adj = reference(P, direct, member, c_cat, allowed, src, avoid)
                res = g.connect(src, dst, allowed, sorted(avoid), k=4)
                want = ref.get(dst)
                assert res["degrees"] == want, (trial, src, dst, res["degrees"], want)
                if want is None:  # no route: one side must have reached everyone it can
                    end = src if res["ran_out"] == "from" else dst
                    reach, _ = reference(P, direct, member, c_cat, allowed, end, avoid)
                    assert res["reached"] == len(reach) - 1, (trial, src, dst, res["reached"], len(reach) - 1)
                for people, links in res["paths"]:
                    assert people[0] == src and people[-1] == dst
                    assert len(people) == want + 1 and len(links) == want
                    assert not (set(people[1:-1]) & avoid)
                    for i, via in enumerate(links):
                        a, b = people[i], people[i + 1]
                        if via == -1:
                            assert any({a, b} == {x, y} and allowed[c] for x, y, c in direct), "bad direct hop"
                        else:
                            assert (a, via) in set(member) and (b, via) in set(member), "bad context hop"
                            assert allowed[c_cat[via]]
                checked += 1
    print(f"ok: {checked} searches match brute-force BFS")


if __name__ == "__main__":
    main()
