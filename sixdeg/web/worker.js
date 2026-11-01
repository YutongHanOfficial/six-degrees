"use strict";
/* The search graph in a Web Worker: the static site's version of sixdeg/graph.py.

   Downloads the graph-*.gz files written by `sixdeg.py site` (keeping them in the Cache API, so
   it happens once per data version), rebuilds the same CSR arrays, and runs the same
   level-synchronous bidirectional BFS with the same scoring, in float32 like numpy.

   People are numbered densely over those with at least one link ("graph ids", in id order);
   messages in and out use database person ids. Contexts use the site's context ids. */

const f32 = Math.fround;
const ROOT = -0x80000000;  // lnk[] of a start node; otherwise lnk[] >= 0 is the context a node was
                           // reached through (parent = cpar[ctx]), and < 0 is -(parent + 1) for a direct link
const BLOCKED = 100;       // dist[] of avoided people
// score bonus for a direct link, by category code (graph.py DIRECT_BONUS)
const BONUS = new Float32Array([1.5, 1, 1, 1, 1, 1, 1, 1, 0.5, 0, 0]);

let G = null;
let ready = null;

self.onmessage = async (e) => {
  const msg = e.data;
  if (msg.type === "load") {
    ready = load(msg).catch((err) => {
      post({ type: "error", error: err instanceof RangeError ? "Not enough memory on this device to load the network." : String(err.message || err) });
      throw err;
    });
    return;
  }
  if (msg.type === "connect") {
    try {
      await ready;
      post({ type: "result", id: msg.id, result: connect(msg) });
    } catch (err) {
      post({ type: "result", id: msg.id, error: String(err.message || err) });
    }
  }
};

function post(m) { self.postMessage(m); }

/* ------------------------------------------------------------ loading */

// The graph files of the current build stay cached; anything else (an older build) is dropped.
async function openCache(keep) {
  try {
    const cache = await caches.open("sixdeg-graph");
    for (const req of await cache.keys()) if (!keep.has(req.url)) await cache.delete(req);
    return cache;
  } catch (e) {
    return null;  // no Cache API (private window, http://): just download every time
  }
}

async function download(url, cache, onBytes) {
  let res = cache && (await cache.match(url).catch(() => null));
  const cached = !!res;
  if (!res) {
    res = await fetch(url);
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const chunks = [];
  let n = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    n += value.length;
    onBytes(value.length);
  }
  const packed = new Uint8Array(n);
  let at = 0;
  for (const c of chunks) { packed.set(c, at); at += c.length; }
  if (cache && !cached) cache.put(url, new Response(packed.slice())).catch(() => {});
  const stream = new Blob([packed]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

// Unsigned LEB128 varints (below 2^31) -> out[at..at+n). Returns the position after the last one.
function varints(bytes, pos, n, out, at = 0) {
  for (let i = 0; i < n; i++) {
    let b = bytes[pos++], x = b & 0x7f;
    if (b & 0x80) {
      b = bytes[pos++];
      x |= (b & 0x7f) << 7;
      if (b & 0x80) {
        b = bytes[pos++];
        x |= (b & 0x7f) << 14;
        if (b & 0x80) {
          b = bytes[pos++];
          x |= (b & 0x7f) << 21;
          if (b & 0x80) { b = bytes[pos++]; x += (b & 0x7f) * 268435456; }
        }
      }
    }
    out[at + i] = x;
  }
  return pos;
}

async function load({ base, version, graph }) {
  const t0 = performance.now();
  const total = graph.files.reduce((s, f) => s + f.bytes, 0);
  let loaded = 0, lastPost = 0;
  const onBytes = (n) => {
    loaded += n;
    const now = performance.now();
    if (now - lastPost > 120 || loaded === total) {
      lastPost = now;
      post({ type: "progress", loaded, total });
    }
  };
  const url = (name) => new URL(`${name}?v=${version}`, base).href;
  const cache = await openCache(new Set(graph.files.map((f) => url(f.name))));
  const files = {};
  await Promise.all(graph.files.map(async (f) => { files[f.name] = await download(url(f.name), cache, onBytes); }));
  post({ type: "progress", loaded: total, total, decoding: true });
  const t1 = performance.now();
  G = build(files, graph);
  const t2 = performance.now();
  post({ type: "ready", ms: Math.round(t2 - t0), phases: { download: Math.round(t1 - t0), build: Math.round(t2 - t1) } });
}

// The arrays graph.py loads from its .npy files, rebuilt from the downloaded varint streams. (The
// loops live in plain functions: V8 optimises long loops inside async functions poorly.)
function build(files, graph) {
  const L = graph.linked, C = graph.contexts, M = graph.memberships, D = graph.direct;
  const { dbId, pScore } = decodePeople(files["graph-persons.gz"], L);
  const { cpPtr, cScore, cCat, size } = decodeContexts(files["graph-contexts.gz"], C, graph.cat_start);
  const cpPer = new Int32Array(M);
  for (const part of graph.members) {
    varints(files[part.name], 0, part.count, cpPer, cpPtr[part.contexts[0]]);
    files[part.name] = null;
    deltasToIds(cpPer, cpPtr, part.contexts[0], part.contexts[1]);
  }
  const [pcPtr, pcCtx, cost] = personContexts(cpPer, cpPtr, size, L, C, M);
  const [dPtr, dNbr, dCat] = decodeDirect(files["graph-direct.gz"], L, D);
  for (let p = 0; p < L; p++) cost[p] += dPtr[p + 1] - dPtr[p];
  return { L, C, dbId, pScore, cpPtr, cScore, cCat, cpPer, pcPtr, pcCtx, dPtr, dNbr, dCat, cost,
           sides: [newSide(L, C), newSide(L, C)] };
}

// log1p(i) for small integers, as float64 (scores are rounded to float32 when stored, like numpy's)
const LOG1P = Float64Array.from({ length: 4096 }, (_, i) => Math.log1p(i));
const LOG1P_HALF = Float64Array.from({ length: 4096 }, (_, i) => Math.log1p(i / 2));

// linked people as gaps between their database ids, then their sitelinks
function decodePeople(bytes, L) {
  const gaps = new Int32Array(L);
  const pos = varints(bytes, 0, L, gaps);
  const dbId = new Int32Array(L);
  for (let i = 0, prev = -1; i < L; i++) dbId[i] = prev = prev + 1 + gaps[i];
  varints(bytes, pos, L, gaps);
  const pScore = new Float32Array(L);
  for (let i = 0; i < L; i++) pScore[i] = gaps[i] < 4096 ? LOG1P[gaps[i]] : Math.log1p(gaps[i]);
  return { dbId, pScore };
}

// per context: sitelinks, then sizes; the category follows from the id ranges
function decodeContexts(bytes, C, catStart) {
  const sl = new Int32Array(C), size = new Int32Array(C);
  varints(bytes, varints(bytes, 0, C, sl), C, size);
  const cpPtr = new Int32Array(C + 1), cScore = new Float32Array(C), cCat = new Uint8Array(C);
  for (let c = 0; c < C; c++) {
    cpPtr[c + 1] = cpPtr[c] + size[c];
    const n = size[c], k = sl[c];
    cScore[c] = 0.4 * (k < 4096 ? LOG1P[k] : Math.log1p(k)) - 0.6 * (n < 4096 ? LOG1P_HALF[n] : Math.log1p(n / 2));
  }
  for (let k = 0; k + 1 < catStart.length; k++) cCat.fill(k, catStart[k], catStart[k + 1]);
  return { cpPtr, cScore, cCat, size };
}

// each context's members arrive as ascending ids stored as deltas
function deltasToIds(cpPer, cpPtr, c0, c1) {
  for (let c = c0; c < c1; c++) {
    for (let e = cpPtr[c] + 1, end = cpPtr[c + 1]; e < end; e++) cpPer[e] += cpPer[e - 1];
  }
}

// person -> contexts, each row ascending; and the summed size of each person's contexts, the main
// part of the "cost" of expanding them (graph.py's Graph.cost)
function personContexts(cpPer, cpPtr, size, L, C, M) {
  const pcPtr = new Int32Array(L + 1);
  for (let e = 0; e < M; e++) pcPtr[cpPer[e] + 1]++;
  for (let p = 0; p < L; p++) pcPtr[p + 1] += pcPtr[p];
  const pcCtx = new Int32Array(M), fill = pcPtr.slice(0, L), cost = new Int32Array(L);
  for (let c = 0; c < C; c++) {
    const n = size[c];
    for (let e = cpPtr[c], end = cpPtr[c + 1]; e < end; e++) {
      const p = cpPer[e];
      pcCtx[fill[p]++] = c;
      cost[p] += n;
    }
  }
  return [pcPtr, pcCtx, cost];
}

// direct links: pairs a < b sorted by (a, b, category); rows come out sorted by (neighbour, category)
function decodeDirect(bytes, L, D) {
  const A = new Int32Array(D), B = new Int32Array(D);
  const pos = varints(bytes, varints(bytes, 0, D, A), D, B);
  const cat = bytes.subarray(pos, pos + D);
  for (let i = 0, a = 0, b = 0; i < D; i++) {
    const da = A[i];
    a += da;
    b = i === 0 || da > 0 ? a + B[i] : b + B[i];
    A[i] = a;
    B[i] = b;
  }
  const dPtr = new Int32Array(L + 1);
  for (let i = 0; i < D; i++) { dPtr[A[i] + 1]++; dPtr[B[i] + 1]++; }
  for (let p = 0; p < L; p++) dPtr[p + 1] += dPtr[p];
  const dNbr = new Int32Array(2 * D), dCat = new Uint8Array(2 * D), at = dPtr.slice(0, L);
  for (let i = 0; i < D; i++) { const e = at[B[i]]++; dNbr[e] = A[i]; dCat[e] = cat[i]; }
  for (let i = 0; i < D; i++) { const e = at[A[i]]++; dNbr[e] = B[i]; dCat[e] = cat[i]; }
  return [dPtr, dNbr, dCat];
}

function newSide(L, C) {
  return {
    dist: new Int8Array(L).fill(-1),
    lnk: new Int32Array(L),
    best: new Float32Array(L),
    cseen: new Uint8Array(C),   // 0 never, 2 entered at the current level, 1 entered earlier
    cbest: new Float32Array(C),
    cpar: new Int32Array(C),
    order: new Int32Array(1 << 16),  // every reached person, level by level; the frontier is the tail
    ctx: new Int32Array(1 << 14),    // every context entered
    fStart: 0, fEnd: 0, nCtx: 0, depth: 0, blocked: [],
  };
}

function grow(a) {
  const b = new Int32Array(a.length * 2);
  b.set(a);
  return b;
}

/* ------------------------------------------------------------ search */

function gidOf(dbId) {
  const ids = G.dbId;
  let lo = 0, hi = ids.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ids[mid] === dbId) return mid;
    if (ids[mid] < dbId) lo = mid + 1; else hi = mid - 1;
  }
  return -1;
}

function connect({ src, dst, allowed, avoid = [], k = 3, maxDegrees = 12 }) {
  const t0 = performance.now();
  if (src === dst) return { degrees: 0, paths: [{ people: [src], links: [] }], visited: 0, ms: 0 };
  const s = gidOf(src), t = gidOf(dst);
  if (s < 0 || t < 0) return { degrees: null, paths: [], visited: 2, ms: 0 };  // someone with no links
  const okCat = Uint8Array.from(allowed, Boolean);
  const [S, T] = G.sides;
  let out = { degrees: null, paths: [] };
  try {
    start(S, s);
    start(T, t);
    for (const a of new Set(avoid)) {
      const v = gidOf(a);
      if (v < 0 || v === s || v === t) continue;
      for (const side of [S, T]) { side.dist[v] = BLOCKED; side.blocked.push(v); }
    }
    while (S.fEnd > S.fStart && T.fEnd > T.fStart && S.depth + T.depth < maxDegrees) {
      const [side, other] = frontierCost(S) <= frontierCost(T) ? [S, T] : [T, S];
      expand(side, okCat);
      const hits = [];
      for (let i = side.fStart; i < side.fEnd; i++) {
        const v = side.order[i];
        if (other.dist[v] >= 0) hits.push(v);
      }
      if (hits.length) {
        out = { degrees: S.depth + T.depth, paths: paths(S, T, hits, k) };
        break;
      }
    }
    out.visited = S.fEnd + S.blocked.length + T.fEnd + T.blocked.length;
  } finally {
    reset(S);
    reset(T);
  }
  for (const p of out.paths) p.people = p.people.map((g) => G.dbId[g]);
  out.ms = Math.round(performance.now() - t0);
  return out;
}

function start(side, node) {
  side.dist[node] = 0;
  side.lnk[node] = ROOT;
  side.best[node] = G.pScore[node];
  side.order[0] = node;
  side.fStart = 0;
  side.fEnd = 1;
  side.nCtx = 0;
  side.depth = 0;
  side.blocked = [];
}

function reset(side) {
  for (let i = 0; i < side.fEnd; i++) side.dist[side.order[i]] = -1;
  for (const v of side.blocked) side.dist[v] = -1;
  for (let i = 0; i < side.nCtx; i++) side.cseen[side.ctx[i]] = 0;
  side.fStart = side.fEnd = side.nCtx = 0;
  side.blocked = [];
}

function frontierCost(side) {
  let s = 0;
  for (let i = side.fStart; i < side.fEnd; i++) s += G.cost[side.order[i]];
  return s;
}

// One BFS level. A person reached this level keeps its best-scoring way in (the last of equal
// ones, as numpy's fancy assignment does); a context is entered once, from its best frontier member.
function expand(side, okCat) {
  const { dPtr, dNbr, dCat, pcPtr, pcCtx, cpPtr, cpPer, cCat, pScore, cScore } = G;
  const { dist, lnk, best, cseen, cbest, cpar } = side;
  const nd = side.depth + 1, fS = side.fStart, fE = side.fEnd;
  let order = side.order, n = fE;

  for (let i = fS; i < fE; i++) {
    const u = order[i], bu = best[u], back = -u - 1;
    for (let e = dPtr[u], end = dPtr[u + 1]; e < end; e++) {
      const c = dCat[e];
      if (!okCat[c]) continue;
      const v = dNbr[e], dv = dist[v];
      if (dv === -1) {
        dist[v] = nd;
        best[v] = f32(f32(bu + BONUS[c]) + pScore[v]);
        lnk[v] = back;
        if (n === order.length) order = side.order = grow(order);
        order[n++] = v;
      } else if (dv === nd) {
        const sc = f32(f32(bu + BONUS[c]) + pScore[v]);
        if (sc >= best[v]) { best[v] = sc; lnk[v] = back; }
      }
    }
  }

  let ctx = side.ctx, nc = side.nCtx;
  const c0 = nc;
  for (let i = fS; i < fE; i++) {
    const u = order[i], bu = best[u];
    for (let e = pcPtr[u], end = pcPtr[u + 1]; e < end; e++) {
      const c = pcCtx[e], st = cseen[c];
      if (st === 1) continue;
      if (st === 0) {
        if (!okCat[cCat[c]]) continue;
        cseen[c] = 2;
        cbest[c] = bu;
        cpar[c] = u;
        if (nc === ctx.length) ctx = side.ctx = grow(ctx);
        ctx[nc++] = c;
      } else if (bu >= cbest[c]) {
        cbest[c] = bu;
        cpar[c] = u;
      }
    }
  }
  ctx.subarray(c0, nc).sort();
  for (let j = c0; j < nc; j++) {
    const c = ctx[j];
    cseen[c] = 1;
    const base = f32(best[cpar[c]] + cScore[c]);
    for (let e = cpPtr[c], end = cpPtr[c + 1]; e < end; e++) {
      const v = cpPer[e], dv = dist[v];
      if (dv === -1) {
        dist[v] = nd;
        best[v] = f32(base + pScore[v]);
        lnk[v] = c;
        if (n === order.length) order = side.order = grow(order);
        order[n++] = v;
      } else if (dv === nd) {
        const sc = f32(base + pScore[v]);
        if (sc >= best[v]) { best[v] = sc; lnk[v] = c; }
      }
    }
  }
  side.nCtx = nc;
  order.subarray(fE, n).sort();
  side.fStart = fE;
  side.fEnd = n;
  side.depth = nd;
}

// Up to k distinct routes through the best-scoring meeting points.
function paths(S, T, hits, k) {
  const total = hits.map((h) => f32(f32(S.best[h] + T.best[h]) - G.pScore[h]));
  const order = hits.map((_, i) => i).sort((a, b) => total[b] - total[a] || a - b);
  const out = [], seen = new Set();
  for (const i of order.slice(0, Math.max(k * 8, 20))) {
    const h = hits[i];
    const fwd = walk(S, h).reverse();
    const back = walk(T, h);
    const people = fwd.map((x) => x[0]).concat(back.slice(1).map((x) => x[0]));
    const links = fwd.slice(1).map((x) => x[1]).concat(back.slice(0, -1).map((x) => x[1]));
    const key = people.join(",");
    if (!seen.has(key)) {
      seen.add(key);
      out.push({ people, links });
    }
    if (out.length >= k) break;
  }
  return out;
}

// [[node, link into node], [parent, link], ..., [root, -2]]; links: -1 direct, else context id
function walk(side, node) {
  const out = [];
  for (;;) {
    const v = side.lnk[node];
    if (v === ROOT) { out.push([node, -2]); return out; }
    if (v >= 0) { out.push([node, v]); node = side.cpar[v]; }
    else { out.push([node, -1]); node = -v - 1; }
  }
}
