"use strict";
/* Static engine: answers the same questions as the Python server's /api/... endpoints, from the
   files `sixdeg.py site` writes, so the app runs on any static host. Small gzip'd JSON shards are
   read out of the big data files with HTTP range requests; the connection search itself runs in
   worker.js. Sentences are ports of sixdeg/store.py.

   window.SixDeg = { call(path, params, opts), network, onNetwork(callback) } */
(function () {
  const CFG = window.SIXDEG_STATIC;
  if (!CFG) return;
  const BASE = new URL(CFG.data, location.href);
  let V = CFG.version;  // replaced by the version in meta.json, which is always fetched fresh
  const url = (name) => new URL(`${name}?v=${V}`, BASE).href;
  const PER_GROUP = 12;     // contexts listed per category in a person's panel
  const MAX_SHARDS = 40;    // a name query never reads more name-index shards than this

  let metaP = null;
  const meta = () => (metaP ||= fetch(new URL("meta.json", BASE).href, { cache: "no-cache" }).then(async (r) => {
    if (!r.ok) throw new Error(`Could not load the database index (HTTP ${r.status})`);
    const m = await r.json();
    V = m.version;
    return m;
  }));

  /* ---------------------------------------------------------- shards */

  async function gunzipText(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Response(stream).text();
  }

  const wholeFiles = new Map();  // for servers that ignore Range: each part file is fetched once

  async function readRange(u, start, end) {
    if (wholeFiles.has(u)) return (await wholeFiles.get(u)).subarray(start, end);
    const res = await fetch(u, { headers: { Range: `bytes=${start}-${end - 1}` } });
    if (res.status === 206) return new Uint8Array(await res.arrayBuffer());
    if (!res.ok) throw new Error(`Could not read ${u} (HTTP ${res.status})`);
    const all = res.arrayBuffer().then((b) => new Uint8Array(b));
    wholeFiles.set(u, all);
    return (await all).subarray(start, end);
  }

  // Code point order (Python's str order), which the shard keys are sorted in.
  function cmp(a, b) {
    const n = Math.min(a.length, b.length);
    for (let i = 0; i < n; i++) {
      let x = a.charCodeAt(i), y = b.charCodeAt(i);
      if (x !== y) {
        if (x >= 0xd800) x = x < 0xe000 ? x + 0x2000 : x - 0x800;
        if (y >= 0xd800) y = y < 0xe000 ? y + 0x2000 : y - 0x800;
        return x - y;
      }
    }
    return a.length - b.length;
  }

  // number of leading items for which pred(item) is true (pred must be true, then false)
  function countWhile(arr, pred) {
    let lo = 0, hi = arr.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pred(arr[mid])) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  class Pack {
    constructor(name) {
      this.name = name;
      this.idx = null;
      this.cache = new Map();
    }

    index() {
      return (this.idx ||= meta().then(async (m) => {
        const res = await fetch(url(m.packs[this.name].index));
        if (!res.ok) throw new Error(`Could not load the ${this.name} index (HTTP ${res.status})`);
        const ix = await res.json();
        const n = ix.len.length;
        let first = ix.first;
        if (ix.delta) {
          first = new Array(n);
          for (let i = 0, acc = 0; i < n; i++) first[i] = acc += ix.first[i];
        }
        const part = new Int32Array(n), off = new Float64Array(n);
        for (let i = 0, p = -1, o = 0; i < n; i++) {
          if (ix.part_start[p + 1] === i) { p++; o = 0; }
          part[i] = p;
          off[i] = o;
          o += ix.len[i];
        }
        return { first, part, off, len: ix.len, parts: ix.parts };
      }));
    }

    shard(i) {
      let p = this.cache.get(i);
      if (!p) {
        p = this.index().then(async (ix) => {
          const bytes = await readRange(url(ix.parts[ix.part[i]]), ix.off[i], ix.off[i] + ix.len[i]);
          return JSON.parse(await gunzipText(bytes));
        });
        p.catch(() => this.cache.delete(i));
        this.cache.set(i, p);
        if (this.cache.size > 4000) this.cache.delete(this.cache.keys().next().value);
      }
      return p;
    }

    // record `key` of a pack whose keys are dense integers (cards, links, contexts)
    async get(key) {
      const ix = await this.index();
      const i = countWhile(ix.first, (f) => f <= key) - 1;
      if (i < 0) return undefined;
      return (await this.shard(i))[key - ix.first[i]];
    }
  }

  const packs = Object.fromEntries(["cards", "links", "contexts", "qid", "search", "searchtop"].map((n) => [n, new Pack(n)]));

  /* ---------------------------------------------------------- people */

  function years(b, d) {
    if (b == null && d == null) return "";
    const f = (y) => (y < 0 ? `${-y} BC` : String(y));
    if (d == null) return `b. ${f(b)}`;
    return `${b != null ? f(b) : "?"}–${f(d)}`;
  }

  function makeCard(id, r) {
    const [q, name, desc, birth, death, gender, sitelinks, image, wiki, nRel, nCtx] = r;
    const enwiki = wiki === 0 ? null : wiki === 1 ? name : wiki;
    return {
      id, qid: "Q" + q, name, description: desc || null, birth_year: birth, death_year: death,
      gender: gender || null, sitelinks, image: image || null, enwiki, n_relations: nRel, n_contexts: nCtx,
      years: years(birth, death),
      // file names are stored URL-encoded already (as Wikidata gives them)
      image_url: image ? `https://commons.wikimedia.org/wiki/Special:FilePath/${image}?width=160` : null,
      wikipedia: enwiki ? "https://en.wikipedia.org/wiki/" + encodeURIComponent(enwiki.replace(/ /g, "_")).replace(/%2F/g, "/") : null,
      wikidata: "https://www.wikidata.org/wiki/Q" + q,
    };
  }

  async function card(id) {
    const r = await packs.cards.get(id);
    return r ? makeCard(id, r) : null;
  }

  async function cardsFor(ids) {
    const uniq = [...new Set(ids)];
    const got = await Promise.all(uniq.map(card));
    return new Map(uniq.map((id, i) => [id, got[i]]));
  }

  const mini = (p) => ({ qid: p.qid, name: p.name, description: p.description, years: p.years, image_url: p.image_url });

  async function byQid(text) {
    const m = /^[Qq](\d+)$/.exec(String(text || "").trim());
    if (!m) return null;
    const q = Number(m[1]);
    const ix = await packs.qid.index();
    const i = countWhile(ix.first, (f) => f <= q) - 1;
    if (i < 0) return null;
    const [deltas, ids] = await packs.qid.shard(i);
    for (let j = 0, acc = ix.first[i]; j < deltas.length; j++) {
      acc += deltas[j];
      if (acc === q) return card(ids[j]);
      if (acc > q) break;
    }
    return null;
  }

  /* ---------------------------------------------------------- name search */

  const MN = /\p{Mn}/gu;
  const WORD = /[\p{L}\p{N}\p{Mc}\p{Me}]+/gu;
  // same rules as tokens() in sixdeg/static_site.py
  const tokens = (text) => text.normalize("NFKD").replace(MN, "").toLowerCase().match(WORD) || [];
  const matches = (term, tok) => (term.prefix ? tok.startsWith(term.t) : tok === term.t);

  // shards that can hold entries for `term`: [start, end] or null
  function span(first, term) {
    const below = countWhile(first, (f) => cmp(f, term.t) < 0);
    const upto = term.prefix
      ? countWhile(first, (f) => cmp(f, term.t) < 0 || f.startsWith(term.t))
      : countWhile(first, (f) => cmp(f, term.t) <= 0);
    if (upto === 0) return null;
    return [Math.max(0, below - 1), upto - 1];
  }

  async function topMatches(key) {
    const ix = await packs.searchtop.index();
    const i = countWhile(ix.first, (f) => cmp(f, key) <= 0) - 1;
    if (i < 0) return null;
    const hit = (await packs.searchtop.shard(i)).find((x) => x[0] === key);
    return hit ? hit[1] : null;
  }

  // Every word must match; the last one may be a prefix (search-as-you-type) -- a single letter
  // only when another word narrows the search ("kevin b"). Best known (lowest id) first, like the
  // FTS query in store.py.
  async function search(text, limit = 8) {
    text = String(text || "").trim();
    if (!text) return [];
    if (/^[Qq]\d+$/.test(text)) {
      const p = await byQid(text);
      return p ? [p] : [];
    }
    const toks = tokens(text);
    if (!toks.length) return [];
    const terms = toks.map((t, i) => ({ t, prefix: i === toks.length - 1 && (t.length > 1 || toks.length > 1) }));
    let ids = null;
    if (terms.length === 1) ids = await topMatches((terms[0].prefix ? "p:" : "e:") + terms[0].t);
    if (!ids) {
      const ix = await packs.search.index();
      const spans = terms.map((t) => span(ix.first, t));
      if (spans.some((s) => !s)) return [];
      let d = 0;
      spans.forEach((s, i) => { if (s[1] - s[0] < spans[d][1] - spans[d][0]) d = i; });
      const [s0, s1] = spans[d];
      if (s1 - s0 + 1 > MAX_SHARDS) return [];
      const driver = terms[d], rest = terms.filter((_, i) => i !== d);
      const shards = await Promise.all(Array.from({ length: s1 - s0 + 1 }, (_, i) => packs.search.shard(s0 + i)));
      const found = new Set();
      for (const shard of shards) {
        for (const [tok, deltas, others] of shard) {
          if (!matches(driver, tok)) continue;
          for (let i = 0, id = 0; i < deltas.length; i++) {
            id += deltas[i];
            if (rest.length) {
              const words = others[i] ? others[i].split(" ") : [];
              words.push(tok);
              if (!rest.every((term) => words.some((w) => matches(term, w)))) continue;
            }
            found.add(id);
          }
        }
      }
      ids = [...found].sort((a, b) => a - b);
    }
    ids = ids.slice(0, limit);
    const cards = await cardsFor(ids);
    return ids.map((id) => cards.get(id)).filter(Boolean);
  }

  async function resolve(text) {
    return (await byQid(text)) || (await search(text, 1))[0] || null;
  }

  /* ---------------------------------------------------------- links and contexts */

  async function linksOf(id) {
    const [m, r] = await Promise.all([meta(), packs.links.get(id)]);
    if (!r) return { rels: [], mems: [] };
    const keys = m.categories.map((c) => c.key);
    let w = 0;
    return {
      rels: r[0].map(([other, type, isA, cat, year, detail]) => (
        { other, type: m.relation_types[type], prio: type, isA: !!isA, category: keys[cat], year, detail })),
      mems: r[1].map(([d, role, fold]) => ({ ctx: (w += d), role: m.roles[role], fold: fold || 0 })),
    };
  }

  function categoryOf(m, ctxId) {
    const cs = m.graph.cat_start;
    return m.categories[countWhile(cs, (s) => s <= ctxId) - 1].key;
  }

  async function contextOf(w) {
    const [m, r] = await Promise.all([meta(), packs.contexts.get(w)]);
    const [kind, q, name, year, period, sitelinks, size, first] = r;
    return { id: w, kind: m.kinds[kind], category: categoryOf(m, w), qid: "Q" + q, name, year, period, sitelinks, size, first };
  }

  const groupTitle = (c, periods) =>
    c.kind === "team" && periods.length ? `${c.name} (${seasonRanges(periods)})`
      : c.kind === "award" && periods.length ? `${c.name} (${periods.join(", ")})`
        : contextTitle(c);
  const sortedPeriods = (cs) => [...new Set(cs.map((c) => c.period).filter((p) => p != null))].sort((a, b) => a - b);

  // Everything two people were part of together, best known first; a team's seasons and an
  // award's years merged into one entry (store.shared)
  async function sharedGroups(a, b, cats, la, lb) {
    const m = await meta();
    const inB = new Set(lb.mems.map((x) => x.ctx));
    const ids = la.mems.filter((x) => inB.has(x.ctx) && cats.has(categoryOf(m, x.ctx))).map((x) => x.ctx);
    const ctxs = (await Promise.all(ids.map(contextOf))).sort((x, y) => y.sitelinks - x.sitelinks || x.id - y.id);
    const groups = new Map();
    for (const c of ctxs) {
      const key = c.kind === "team" || c.kind === "award" ? `${c.kind}:${c.qid}` : c.id;
      if (!groups.has(key)) groups.set(key, { row: c, ids: [], rows: [] });
      groups.get(key).ids.push(c.id);
      groups.get(key).rows.push(c);
    }
    return [...groups.values()].map((g) => ({ ...g, title: groupTitle(g.row, sortedPeriods(g.rows)) }));
  }

  const roleSet = (l, ctx) => new Set((l.mems.find((x) => x.ctx === ctx)?.role || "").split(","));

  async function explainLink(a, b, via, cats, cardsP, labels) {
    const [la, lb] = await Promise.all([linksOf(a), linksOf(b)]);
    let shared = await sharedGroups(a, b, cats, la, lb);
    const [ctx, cards] = await Promise.all([via >= 0 ? contextOf(via) : null, cardsP]);
    const A = cards.get(a), B = cards.get(b);
    let out;
    if (ctx) {
      const main = shared.find((g) => g.ids.includes(via));
      const title = main ? main.title : contextTitle(ctx);
      out = {
        kind: "context", category: ctx.category,
        sentence: contextSentence(ctx, A, B, roleSet(la, via), roleSet(lb, via), title),
        context: { qid: ctx.qid, name: ctx.name, year: ctx.year, kind: ctx.kind, period: ctx.period, size: ctx.size },
      };
      shared = shared.filter((g) => g !== main);
    } else {
      const rels = la.rels.filter((r) => r.other === b)
        .sort((x, y) => (!cats.has(x.category)) - (!cats.has(y.category)) || x.prio - y.prio);
      const r = rels[0] || { type: null, category: "other", isA: true, prio: 99 };
      out = {
        kind: "relation", category: r.category, type: r.type,
        sentence: relationSentence({ type: r.type, a_id: r.isA ? a : b, detail: r.detail, year: r.year }, A, B),
      };
    }
    out.category_label = labels[out.category];
    out.also = { count: shared.length, items: shared.slice(0, 4).map((g) => g.title) };
    return out;
  }

  /* ---------------------------------------------------------- the search graph (worker.js) */

  const network = { ready: false, loaded: 0, total: 0, decoding: false, error: null };
  const listeners = new Set();
  const emit = () => listeners.forEach((f) => f(network));
  let worker = null, networkP = null, seq = 0;
  const pending = new Map();

  function startNetwork() {
    return (networkP ||= meta().then((m) => new Promise((resolve, reject) => {
      network.total = m.graph.files.reduce((s, f) => s + f.bytes, 0);
      emit();
      const fail = (msg) => {
        network.error = msg;
        emit();
        reject(new Error(msg));
        for (const p of pending.values()) p.reject(new Error(msg));
        pending.clear();
      };
      if (typeof DecompressionStream === "undefined") return fail("This browser is too old for the search (it needs DecompressionStream).");
      worker = new Worker(new URL(`worker.js?v=${CFG.web || V}`, location.href));
      worker.onmessage = (e) => {
        const d = e.data;
        if (d.type === "progress") {
          Object.assign(network, { loaded: d.loaded, total: d.total, decoding: !!d.decoding });
          emit();
        } else if (d.type === "ready") {
          Object.assign(network, { ready: true, decoding: false, ms: d.ms });
          emit();
          resolve();
        } else if (d.type === "error") {
          fail(d.error);
        } else if (d.type === "searching") {
          pending.get(d.id)?.onProgress?.({ degrees: d.degrees, visited: d.visited });
        } else if (d.type === "result") {
          const p = pending.get(d.id);
          pending.delete(d.id);
          if (p) d.error ? p.reject(new Error(d.error)) : p.resolve(d.result);
        }
      };
      worker.onerror = (e) => fail(e.message || "The search worker stopped.");
      worker.postMessage({ type: "load", base: BASE.href, version: m.version, graph: m.graph });
    })));
  }

  // Rejects with an AbortError as soon as `signal` fires (also while the network is still
  // downloading); the worker drops the search at its next pause.
  async function graphConnect(args, { signal, onProgress } = {}) {
    const stopped = new Promise((_, reject) => {
      const stop = () => reject(Object.assign(new Error("Search stopped"), { name: "AbortError" }));
      if (signal?.aborted) stop(); else signal?.addEventListener("abort", stop, { once: true });
    });
    stopped.catch(() => {});
    await Promise.race([startNetwork(), stopped]);
    const id = ++seq;
    const answer = new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject, onProgress });
      worker.postMessage({ type: "connect", id, ...args });
    });
    stopped.catch(() => { if (pending.delete(id)) worker.postMessage({ type: "cancel", id }); });
    return Promise.race([answer, stopped]);
  }

  /* ---------------------------------------------------------- API */

  // opts: { signal (AbortSignal), onProgress({ degrees, visited }) } -- see graphConnect
  async function apiConnect(q, opts) {
    const [A, B] = await Promise.all([resolve(q.from), resolve(q.to)]);
    if (!A || !B) throw new Error(`No person found for “${!A ? q.from : q.to}”.`);
    const m = await meta();
    const keys = m.categories.map((c) => c.key);
    const labels = Object.fromEntries(m.categories.map((c) => [c.key, c.label]));
    const chosen = q.cats ? new Set(String(q.cats).split(",").filter((k) => keys.includes(k)))
      : new Set(m.categories.filter((c) => c.default).map((c) => c.key));
    const avoid = (await Promise.all(String(q.avoid || "").split(",").filter(Boolean).map(byQid))).filter(Boolean);
    const res = await graphConnect({
      src: A.id, dst: B.id, allowed: keys.map((k) => chosen.has(k)), avoid: avoid.map((p) => p.id),
      k: Math.min(Number(q.k) || 4, 10),
    }, opts);
    const out = { from: A, to: B, degrees: res.degrees, ms: res.ms, visited: res.visited,
                  categories: [...chosen].sort(), avoid, paths: [] };
    if (res.degrees === null) {
      const lonely = [A, B].filter((p) => p.n_relations + p.n_contexts === 0).map((p) => p.name);
      out.reason = lonely.length
        ? `${lonely.join(" and ")} ${lonely.length === 1 ? "has" : "have"} no recorded connections in the database.`
        : noRoute(res.ranOut === "to" ? B : A, res.ranOut === "to" ? A : B, res.reached, avoid.length > 0);
      return out;
    }
    opts?.onProgress?.({ found: res.degrees, visited: res.visited });
    const cardsP = cardsFor(res.paths.flatMap((p) => p.people));  // fetched alongside the link details
    out.paths = await Promise.all(res.paths.map(async (p) => {
      const links = await Promise.all(p.links.map((via, i) => explainLink(p.people[i], p.people[i + 1], via, chosen, cardsP, labels)));
      const cards = await cardsP;
      return { people: p.people.map((id) => cards.get(id)), links };
    }));
    return out;
  }

  // The search reached everyone `who` is connected to (with these settings) without finding `other`
  // (server.py no_route)
  function noRoute(who, other, reached, avoiding) {
    if (!reached) return `None of ${who.name}'s links can be used with these ${avoiding ? "settings" : "link types"}.`;
    return `${who.name}'s whole network is ${reached.toLocaleString("en-US")} other ${reached === 1 ? "person" : "people"}, ` +
      `and ${other.name} isn't one of them.`;
  }

  // A person's direct connections, grouped by link type (store.neighborhood)
  async function apiPerson(q) {
    const p = await resolve(q.id);
    if (!p) throw new Error("not found");
    const m = await meta();
    const labels = Object.fromEntries(m.categories.map((c) => [c.key, c.label]));
    const L = await linksOf(p.id);
    const relByCat = new Map();
    for (const r of L.rels) {
      if (!relByCat.has(r.category)) relByCat.set(r.category, []);
      relByCat.get(r.category).push(r);
    }
    const byCat = new Map();
    for (const x of L.mems) {
      const cat = categoryOf(m, x.ctx);
      if (!byCat.has(cat)) byCat.set(cat, new Map());
      const groups = byCat.get(cat);
      const key = x.fold ? "f" + x.fold : "c" + x.ctx;
      if (!groups.has(key)) groups.set(key, { ids: [], roles: new Set() });
      const g = groups.get(key);
      g.ids.push(x.ctx);
      for (const r of (x.role || "").split(",")) if (r) g.roles.add(r);
    }
    const shown = [...byCat.values()].flatMap((groups) => [...groups.values()].slice(0, PER_GROUP));
    const ctxs = new Map((await Promise.all(shown.flatMap((g) => g.ids).map(contextOf))).map((c) => [c.id, c]));
    for (const g of shown) {
      g.rows = g.ids.map((id) => ctxs.get(id));
      const mates = new Set(g.rows.flatMap((c) => c.first).filter((x) => x !== p.id));
      g.mates = [...mates].sort((a, b) => a - b).slice(0, 4);
    }
    const cards = await cardsFor([...[...relByCat.values()].flatMap((l) => l.slice(0, 25).map((r) => r.other)),
                                  ...shown.flatMap((g) => g.mates)]);
    cards.set(p.id, p);
    const relations = {};
    for (const [cat, list] of relByCat) {
      const items = list.slice(0, 25).map((r) => {
        const other = cards.get(r.other);
        const [a, b] = r.isA ? [p, other] : [other, p];
        return { person: mini(other), sentence: relationSentence({ type: r.type, a_id: a.id, detail: r.detail, year: r.year }, a, b) };
      });
      items.total = list.length;  // only the first 25 are spelled out
      relations[cat] = items;
    }
    const contexts = {};
    for (const [cat, groups] of byCat) {
      const items = [...groups.values()].slice(0, PER_GROUP).map((g) => {
        const c = g.rows[0], periods = sortedPeriods(g.rows);
        return {
          title: groupTitle(c, periods), role: [...g.roles].sort().join(", "),
          size: Math.max(...g.rows.map((x) => x.size)), seasons: c.kind === "team" ? periods.length : null,
          with: g.mates.map((id) => cards.get(id)).filter(Boolean).map(mini),
        };
      });
      contexts[cat] = { total: groups.size, items };
    }
    return { person: p, relations, contexts, labels };
  }

  async function apiRandom() {
    const pool = (await meta()).random_pool;
    const i = Math.floor(Math.random() * pool.length);
    let j = Math.floor(Math.random() * (pool.length - 1));
    if (j >= i) j++;
    return Promise.all([card(pool[i]), card(pool[j])]);
  }

  async function apiStats() {
    const m = await meta();
    return { ...m.stats, categories: m.categories };
  }

  window.SixDeg = {
    network,
    onNetwork(f) { listeners.add(f); f(network); return () => listeners.delete(f); },
    startNetwork,
    async call(path, params = {}, opts = {}) {
      switch (path) {
        case "/api/stats": return apiStats();
        case "/api/search": return search(params.q, Math.min(Number(params.limit) || 8, 25));
        case "/api/connect": return apiConnect(params, opts);
        case "/api/person": return apiPerson(params);
        case "/api/random": return apiRandom();
        default: throw new Error("unknown request " + path);
      }
    },
  };
  startNetwork().catch(() => {});
  // the shard directories (~250 KB in all) are needed by the first search of any kind
  meta().then(() => Object.values(packs).forEach((p) => p.index().catch(() => {}))).catch(() => {});

  /* ---------------------------------------------------------- sentences (ports of store.py) */

  const gendered = (g, male, female, neutral) => (g === "m" ? male : g === "f" ? female : neutral);

  const NEEDS_ARTICLE = new RegExp(
    "\\b(elections?|conference|battle|siege|war|revolution|uprising|treaty|agreement|accords?|pact|act|" +
    "declaration|charter|constitution|manifesto|petition|resolution|convention|congress|council|summit|" +
    "assembly|parliament|senate|committee|commission|society|academy|club|party|group|family|dynasty|house|" +
    "games|olympics|cup|championships?|tournament|final|race|marathon|grand prix|open|trophy|league|" +
    "award|prize|medal|order|cross|honou?rs|expedition|festival|referendum|debate|primary|caucus)\\b", "i");

  function the(name) {
    if (name.toLowerCase().startsWith("the ")) return name;
    if (/^\p{Nd}/u.test(name) || NEEDS_ARTICLE.test(name)) return "the " + name;
    return name;
  }

  function contextTitle(c) {
    const name = c.name;
    if (c.kind === "team" && c.period != null) return `${name} (${seasonRanges([c.period])})`;
    if (c.kind === "award" && c.period != null) return `${name} (${c.period})`;
    if (c.year && ["work", "authored", "election", "event", "document", "spaceflight", "org"].includes(c.kind)) {
      if (!name.includes(String(c.year))) return `${name} (${c.year})`;
    }
    return name;
  }

  function seasonRanges(seasons) {
    const runs = [];
    for (const s of [...new Set(seasons)].sort((a, b) => a - b)) {
      if (runs.length && s === runs[runs.length - 1][1] + 1) runs[runs.length - 1][1] = s;
      else runs.push([s, s]);
    }
    return runs.map(([a, b]) => {
      const end = b + 1;
      return Math.floor(end / 100) === Math.floor(a / 100) ? `${a}–${String(end % 100).padStart(2, "0")}` : `${a}–${end}`;
    }).join(", ");
  }

  function role(roles) {
    for (const r of ["director", "cast member", "voice actor", "screenwriter", "producer", "performer", "composer",
                     "lyricist", "author", "creator", "illustrator", "editor"]) {
      if (roles.has(r)) return r;
    }
    return [...roles].sort()[0] || "contributor";
  }

  function contextSentence(ctx, A, B, ra, rb, title) {
    const a = A.name, b = B.name, kind = ctx.kind, cat = ctx.category;
    const any = (set, xs) => xs.some((x) => set.has(x));
    title = title || contextTitle(ctx);
    let when = title.startsWith(ctx.name) ? title.slice(ctx.name.length).trim() : "";
    if (kind === "work") {
      const acting = ["cast member", "voice actor", "presenter", "talk show guest"];
      if (cat === "film") {
        if (any(ra, acting) && any(rb, acting)) return `${a} and ${b} both appeared in ${title}`;
        if (ra.has("director") && any(rb, acting)) return `${a} directed ${b} in ${title}`;
        if (rb.has("director") && any(ra, acting)) return `${b} directed ${a} in ${title}`;
      }
      if (cat === "music" && ra.has("performer") && rb.has("performer")) return `${a} and ${b} performed together on ${title}`;
      if (ra.has("author") && rb.has("author")) return `${a} and ${b} co-wrote ${title}`;
      return `${a} (${role(ra)}) and ${b} (${role(rb)}) both worked on ${title}`;
    }
    if (kind === "authored") return `${a} and ${b} co-authored “${ctx.name}”` + (ctx.year ? ` (${ctx.year})` : "");
    if (kind === "group") {
      if (cat === "music") return `${a} and ${b} were bandmates in ${ctx.name}`;
      if (cat === "family") return `${a} and ${b} are both members of ${the(ctx.name)}`;
      return `${a} and ${b} were both members of ${the(ctx.name)}`;
    }
    if (kind === "team") {
      const coach = ["head coach", "coach"];
      if (any(ra, coach) && !any(rb, coach)) return `${a} coached ${b} at ${title}`;
      if (any(rb, coach) && !any(ra, coach)) return `${b} coached ${a} at ${title}`;
      if (any(ra, coach) && any(rb, coach)) return `${a} and ${b} were both on the coaching staff of ${title}`;
      return `${a} and ${b} were teammates at ${title}`;
    }
    if (kind === "election") return `${a} and ${b} were both candidates in ${the(title)}`;
    if (kind === "award") {
      const award = the(ctx.name);
      when = when ? ` in ${when.replace(/^[()]+|[()]+$/g, "")}` : "";
      if (ra.has("recipient") && rb.has("recipient")) return `${a} and ${b} both received ${award}${when}`;
      if (ra.has("recipient")) return `${a} won and ${b} was nominated for ${award}${when}`;
      if (rb.has("recipient")) return `${b} won and ${a} was nominated for ${award}${when}`;
      return `${a} and ${b} were both nominated for ${award}${when}`;
    }
    if (kind === "event") return `${a} and ${b} both took part in ${the(title)}`;
    if (kind === "document") return `${a} and ${b} both signed ${the(title)}`;
    if (kind === "org") return `${a} and ${b} co-founded ${title}`;
    if (kind === "spaceflight") return `${a} and ${b} flew together on ${title}`;
    return `${a} and ${b} are both connected to ${title}`;
  }

  const DESCENDANT = /\b(child|children|son|daughter|grandchild|grandson|granddaughter|nephew|niece|descendant)\b/i;
  const ANCESTOR = /\b(parent|father|mother|grandparent|grandfather|grandmother|uncle|aunt|ancestor)\b/i;

  // Wikidata sometimes gives a kinship term from the other person's side; birth years give it away
  function kinshipFlipped(detail, src, dst) {
    const bs = src.birth_year, bd = dst.birth_year;
    if (bs == null || bd == null || detail.includes("in-law")) return false;
    if (DESCENDANT.test(detail)) return bd < bs - 10;
    if (ANCESTOR.test(detail)) return bd > bs + 10;
    return false;
  }

  // A and B in path order; rel.a_id is the relation's "from" side
  function relationSentence(rel, A, B) {
    const { type: t, detail, year } = rel;
    const [src, dst] = rel.a_id === A.id ? [A, B] : [B, A];
    const s = src.name, d = dst.name;
    switch (t) {
      case "spouse": return `${A.name} and ${B.name} ` + (year ? `married in ${year}` : "were married");
      case "partner": return `${A.name} and ${B.name} were partners`;
      case "parent":
        if (src === A) return `${s} is the ${gendered(src.gender, "father", "mother", "parent")} of ${d}`;
        return `${d} is the ${gendered(dst.gender, "son", "daughter", "child")} of ${s}`;
      case "sibling": return `${A.name} is the ${gendered(A.gender, "brother", "sister", "sibling")} of ${B.name}`;
      case "relative":
        if (!detail) return `${A.name} and ${B.name} are relatives`;
        return kinshipFlipped(detail, src, dst) ? `${s} is ${d}'s ${detail}` : `${d} is ${s}'s ${detail}`;
      case "stepparent": return `${s} is the ${gendered(src.gender, "stepfather", "stepmother", "stepparent")} of ${d}`;
      case "godparent": return `${s} is the ${gendered(src.gender, "godfather", "godmother", "godparent")} of ${d}`;
      case "advisor": return `${s} was the doctoral advisor of ${d}`;
      case "teacher": return `${s} taught ${d}`;
      case "influence": return `${d} was influenced by ${s}`;
      case "partner_sport": return `${A.name} and ${B.name} were partners in sport or business`;
      case "significant": return `${d} was a significant person for ${s}` + (detail ? ` (${detail})` : "");
      case "succession": return `${d} succeeded ${s}` + (detail ? ` as ${detail}` : "") + (year ? ` in ${year}` : "");
      default: return `${A.name} and ${B.name} are connected`;
    }
  }

  window.SixDeg._internals = { tokens, cmp, contextSentence, relationSentence, seasonRanges, the, contextTitle };
})();
