"""Shortest-connection search over the people graph.

The graph is bipartite-plus: people link to each other directly (spouse, parent, advisor...)
and through shared contexts (a film's cast, a team's squad in one season, a band...). Going
person -> context -> person counts as ONE degree, exactly like a direct link.

Search is a level-synchronous bidirectional BFS in numpy. Among all routes of the minimal
length it returns the one through the best-known people (sum of log sitelinks) and the
most recognisable links; alternatives are other routes of the same length.
"""
from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

from . import CATEGORIES

ROOT = -2      # via[] value of the start node
DIRECT = -1    # via[] value for a direct person-to-person link
BLOCKED = 32000

# Score bonus for a direct link, by category (index = category code).
_DIRECT_BONUS = {"family": 1.5, "other": 0.5, "awards": 0.0, "influence": 0.0}
DIRECT_BONUS = np.array([_DIRECT_BONUS.get(k, 1.0) for k, _ in CATEGORIES], np.float32)

_E64 = np.zeros(0, np.int64)
_E32 = np.zeros(0, np.int32)


def _gather(ptr, nodes):
    """Concatenated CSR rows of `nodes`: (positions into the column array, owning node)."""
    starts = ptr[nodes]
    lens = ptr[nodes + 1] - starts
    total = int(lens.sum())
    if total == 0:
        return _E64, _E32
    owner = np.repeat(nodes, lens)
    ends = np.cumsum(lens)
    pos = np.arange(total, dtype=np.int64) + np.repeat(starts - (ends - lens), lens)
    return pos, owner


class _Side:
    """BFS state for one end of the search. Arrays persist; only touched slots are reset."""

    def __init__(self, P: int, C: int):
        self.dist = np.full(P, -1, np.int16)
        self.par = np.full(P, -1, np.int32)
        self.via = np.full(P, ROOT, np.int32)
        self.best = np.full(P, -np.inf, np.float32)
        self.new_p = np.zeros(P, bool)
        self.cseen = np.zeros(C, bool)
        self.cbest = np.full(C, -np.inf, np.float32)
        self.cpar = np.full(C, -1, np.int32)
        self.new_c = np.zeros(C, bool)
        self.touched, self.touched_c = [], []
        self.frontier = _E32
        self.depth = 0

    def start(self, node: int, score: float):
        n = np.array([node], np.int32)
        self.dist[n] = 0
        self.via[n] = ROOT
        self.best[n] = score
        self.touched.append(n)
        self.frontier, self.depth = n, 0

    def reset(self):
        for t in self.touched:
            self.dist[t] = -1
            self.par[t] = -1
            self.via[t] = ROOT
            self.best[t] = -np.inf
        for t in self.touched_c:
            self.cseen[t] = False
            self.cbest[t] = -np.inf
            self.cpar[t] = -1
        self.touched, self.touched_c = [], []
        self.frontier, self.depth = _E32, 0


class Graph:
    def __init__(self, graph_dir: str):
        with open(os.path.join(graph_dir, "meta.json")) as f:
            self.meta = json.load(f)
        if self.meta["categories"] != [k for k, _ in CATEGORIES]:
            raise RuntimeError("graph was built with a different category list; rebuild it")

        def load(name):
            return np.load(os.path.join(graph_dir, name + ".npy"))

        self.P, self.C = self.meta["persons"], self.meta["contexts"]
        self.p_score, self.c_cat, self.c_score = load("p_score"), load("c_cat"), load("c_score")
        self.d_ptr, self.d_nbr, self.d_cat = load("d_ptr"), load("d_nbr"), load("d_cat")
        self.pc_ptr, self.pc_ctx = load("pc_ptr"), load("pc_ctx")
        self.cp_ptr, self.cp_per = load("cp_ptr"), load("cp_per")
        # rough cost of expanding a person: direct links + summed size of their contexts
        csum = np.concatenate([[0], np.cumsum(np.diff(self.cp_ptr)[self.pc_ctx])])
        self.cost = np.diff(self.d_ptr) + (csum[self.pc_ptr[1:]] - csum[self.pc_ptr[:-1]])
        self._lock = threading.Lock()
        self._sides = (_Side(self.P, self.C), _Side(self.P, self.C))

    # ------------------------------------------------------------ search
    def connect(self, src: int, dst: int, allowed, avoid=(), k: int = 3, max_degrees: int | None = None):
        """Shortest connections from src to dst.

        allowed: bool array indexed by category code. avoid: person ids the route must not use.
        Returns dict(degrees, paths=[(people, links)], visited, ms); degrees is None if unreachable.
        A path's links[i] joins people[i] and people[i+1]: -1 = direct relation, else context id.
        With no max_degrees the search runs until the two sides meet or one runs out of people
        (bounded by the size of the graph); in that case ran_out is "from" or "to" and reached is
        how many other people that side could reach.
        """
        allowed = np.asarray(allowed, bool)
        t0 = time.time()
        with self._lock:
            S, T = self._sides
            try:
                result = self._search(S, T, src, dst, allowed, avoid, k, max_degrees)
                result["visited"] = int(sum(t.size for t in S.touched) + sum(t.size for t in T.touched))
            finally:
                S.reset()
                T.reset()
        result["ms"] = round((time.time() - t0) * 1000)
        return result

    def _search(self, S, T, src, dst, allowed, avoid, k, max_degrees):
        if src == dst:
            return {"degrees": 0, "paths": [([src], [])]}
        S.start(src, float(self.p_score[src]))
        T.start(dst, float(self.p_score[dst]))
        blocked = np.array([a for a in set(avoid) if a not in (src, dst)], np.int32)
        if blocked.size:
            for side in (S, T):
                side.dist[blocked] = BLOCKED
                side.touched.append(blocked)
        while S.frontier.size and T.frontier.size and (max_degrees is None or S.depth + T.depth < max_degrees):
            if self.cost[S.frontier].sum() <= self.cost[T.frontier].sum():
                side, other = S, T
            else:
                side, other = T, S
            new = self._expand(side, allowed)
            hits = new[other.dist[new] >= 0]
            if hits.size:
                return {"degrees": S.depth + T.depth, "paths": self._paths(S, T, hits, k)}
        out = {"degrees": None, "paths": []}
        for side, end in ((S, "from"), (T, "to")):
            if not side.frontier.size:  # everyone reachable from this end has been checked
                out["ran_out"] = end
                out["reached"] = int(sum(t.size for t in side.touched)) - blocked.size - 1
        return out

    def _expand(self, side: _Side, allowed) -> np.ndarray:
        F, depth = side.frontier, side.depth
        kids, pars, vias, scores = [], [], [], []

        # direct person -> person links
        pos, owner = _gather(self.d_ptr, F)
        if pos.size:
            nbr, cat = self.d_nbr[pos], self.d_cat[pos]
            ok = allowed[cat] & (side.dist[nbr] < 0)
            if ok.any():
                nbr, owner, cat = nbr[ok], owner[ok], cat[ok]
                kids.append(nbr)
                pars.append(owner)
                vias.append(np.full(nbr.size, DIRECT, np.int32))
                scores.append(side.best[owner] + DIRECT_BONUS[cat] + self.p_score[nbr])

        # person -> context -> person
        pos, owner = _gather(self.pc_ptr, F)
        if pos.size:
            ctx = self.pc_ctx[pos]
            ok = allowed[self.c_cat[ctx]] & ~side.cseen[ctx]
            ctx, owner = ctx[ok], owner[ok]
            if ctx.size:
                # each context is entered once, from its best-scoring frontier member
                sc = side.best[owner]
                np.maximum.at(side.cbest, ctx, sc)
                win = sc >= side.cbest[ctx]
                side.cpar[ctx[win]] = owner[win]
                side.new_c[ctx] = True
                uctx = np.flatnonzero(side.new_c).astype(np.int32)
                side.new_c[uctx] = False
                side.cseen[uctx] = True
                side.touched_c.append(uctx)

                pos2, cown = _gather(self.cp_ptr, uctx)
                mem = self.cp_per[pos2]
                ok2 = side.dist[mem] < 0
                mem, cown = mem[ok2], cown[ok2]
                if mem.size:
                    par = side.cpar[cown]
                    kids.append(mem)
                    pars.append(par)
                    vias.append(cown)
                    scores.append(side.best[par] + self.c_score[cown] + self.p_score[mem])

        side.depth = depth + 1
        if not kids:
            side.frontier = _E32
            return _E32
        kid = np.concatenate(kids)
        par = np.concatenate(pars)
        via = np.concatenate(vias)
        score = np.concatenate(scores).astype(np.float32, copy=False)
        # keep the best-scoring way into each newly reached person
        np.maximum.at(side.best, kid, score)
        win = score >= side.best[kid]
        side.par[kid[win]] = par[win]
        side.via[kid[win]] = via[win]
        side.new_p[kid] = True
        new = np.flatnonzero(side.new_p).astype(np.int32)
        side.new_p[new] = False
        side.dist[new] = depth + 1
        side.touched.append(new)
        side.frontier = new
        return new

    def _paths(self, S, T, hits, k):
        total = S.best[hits] + T.best[hits] - self.p_score[hits]
        order = np.argsort(-total, kind="stable")[: max(k * 8, 20)]
        paths, seen = [], set()
        for h in hits[order]:
            fwd = self._walk(S, int(h))[::-1]      # src ... h
            back = self._walk(T, int(h))           # h ... dst
            people = [n for n, _ in fwd] + [n for n, _ in back[1:]]
            links = [v for _, v in fwd[1:]] + [v for _, v in back[:-1]]
            key = tuple(people)
            if key not in seen:
                seen.add(key)
                paths.append((people, links))
            if len(paths) >= k:
                break
        return paths

    @staticmethod
    def _walk(side, node):
        """[(node, via-into-node), (parent, via), ..., (root, ROOT)]"""
        out = []
        while True:
            v = int(side.via[node])
            out.append((node, v))
            if v == ROOT:
                return out
            node = int(side.par[node])
