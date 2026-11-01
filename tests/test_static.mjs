// Runs the static site's browser code (sixdeg/web/worker.js and engine.js) under Node against an
// exported site, and checks it against answers from the Python app.
//
//   python3 tests/static_reference.py refs.json        # graph searches
//   python3 tests/static_reference.py --app app.json   # name search, sentences, panels
//   node tests/test_static.mjs site refs.json app.json
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.join(HERE, "..", "sixdeg", "web");
const [siteDir, refsPath, appPath] = process.argv.slice(2);
const site = path.resolve(siteDir);
const meta = JSON.parse(fs.readFileSync(path.join(site, "data", "meta.json"), "utf8"));
let ranges = 0;

// fetch() for file:// URLs, honouring Range like a static host
async function fileFetch(u, opts = {}) {
  const file = fileURLToPath(String(u).split("?")[0]);
  if (!fs.existsSync(file)) return new Response("not found", { status: 404 });
  const range = opts.headers && opts.headers.Range;
  if (range) {
    const [, a, b] = /bytes=(\d+)-(\d+)/.exec(range);
    const fd = fs.openSync(file, "r");
    const buf = Buffer.alloc(Number(b) - Number(a) + 1);
    fs.readSync(fd, buf, 0, buf.length, Number(a));
    fs.closeSync(fd);
    ranges++;
    return new Response(buf, { status: 206 });
  }
  return new Response(fs.readFileSync(file));
}

const shared = { URL, Blob, Response, DecompressionStream, TextDecoder, performance, console, setTimeout, Math };

// The worker in its own context; messages go through setImmediate like a real Worker's would.
class FakeWorker {
  constructor() {
    const ctx = vm.createContext({ ...shared, fetch: fileFetch });
    ctx.self = ctx;
    ctx.postMessage = (data) => setImmediate(() => this.onmessage && this.onmessage({ data }));
    vm.runInContext(fs.readFileSync(path.join(WEB, "worker.js"), "utf8"), ctx, { filename: "worker.js" });
    this.ctx = ctx;
  }
  postMessage(data) { setImmediate(() => this.ctx.onmessage({ data: structuredClone(data) })); }
}

function check(name, ok, detail = "") {
  if (!ok) failures.push(`${name}${detail ? ": " + detail : ""}`);
}
const failures = [];

/* ---------------------------------------------------------- 1. graph search vs graph.py */

async function testGraph(refs) {
  const w = new FakeWorker();
  const ready = new Promise((res, rej) => {
    w.onmessage = (e) => (e.data.type === "ready" ? res(e.data) : e.data.type === "error" ? rej(new Error(e.data.error)) : 0);
  });
  const t0 = performance.now();
  w.postMessage({ type: "load", base: pathToFileURL(path.join(site, "data") + "/").href, version: meta.version, graph: meta.graph });
  const info = await ready;
  console.log(`graph loaded in ${((performance.now() - t0) / 1000).toFixed(1)}s`, JSON.stringify(info.phases));
  const run = vm.runInContext("({ connect, gidOf, G })", w.ctx);
  const BONUS = [1.5, 1, 1, 1, 1, 1, 1, 1, 0.5, 0, 0];
  const keys = meta.categories.map((c) => c.key);
  let ms = 0, unreachable = 0, longest = 0;
  for (const ref of refs) {
    const allowed = keys.map((k) => ref.cats.includes(k));
    const r = await run.connect({ src: ref.src, dst: ref.dst, allowed, avoid: ref.avoid, k: 3 });
    ms += r.ms;
    const what = `${ref.src}->${ref.dst} [${ref.cats}] avoid ${ref.avoid}`;
    check("degrees", r.degrees === ref.degrees, `${what}: ${r.degrees} vs ${ref.degrees}`);
    check("visited", r.visited === ref.visited, `${what}: ${r.visited} vs ${ref.visited}`);
    check("ran out", (r.ranOut ?? null) === ref.ran_out && (r.reached ?? null) === ref.reached,
          `${what}: ${r.ranOut} ${r.reached} vs ${ref.ran_out} ${ref.reached}`);
    if (ref.degrees === null) unreachable++;
    else longest = Math.max(longest, ref.degrees);
    if (ref.degrees && r.paths.length) {
      const { G } = run;
      const g = r.paths[0].people.map(run.gidOf);
      let s = g.reduce((acc, x) => acc + G.pScore[x], 0);
      r.paths[0].links.forEach((via, i) => {
        if (via >= 0) s += G.cScore[via];
        else {
          let best = -Infinity;
          for (let e = G.dPtr[g[i]]; e < G.dPtr[g[i] + 1]; e++) {
            if (G.dNbr[e] === g[i + 1] && allowed[G.dCat[e]]) best = Math.max(best, BONUS[G.dCat[e]]);
          }
          s += best;
        }
      });
      check("best route score", Math.abs(s - ref.score) < 1e-3, `${what}: ${s} vs ${ref.score}`);
      check("route ends", r.paths[0].people[0] === ref.src && r.paths[0].people.at(-1) === ref.dst, what);
      check("route length", r.paths[0].people.length === ref.degrees + 1, what);
    }
  }
  console.log(`${refs.length} graph searches compared (${(ms / refs.length).toFixed(0)} ms average; ` +
              `longest route ${longest} degrees, ${unreachable} pairs not connected at all)`);

  // A search stopped part-way leaves nothing behind: stop the slowest one after its first pause,
  // then run every search again and check it still agrees.
  const slow = refs.reduce((a, b) => (b.visited > a.visited ? b : a));
  const allowedOf = (ref) => keys.map((k) => ref.cats.includes(k));
  let reports = 0;
  const stopped = await run.connect({ src: slow.src, dst: slow.dst, allowed: allowedOf(slow), avoid: slow.avoid, k: 3, pauseMs: 0 },
                                    () => reports++, () => true);
  check("stop", stopped === null && reports === 1, `${stopped} after ${reports} reports`);
  for (const ref of refs.slice(0, 60)) {
    const r = await run.connect({ src: ref.src, dst: ref.dst, allowed: allowedOf(ref), avoid: ref.avoid, k: 3 });
    check("after a stop", r.degrees === ref.degrees && r.visited === ref.visited, `${ref.src}->${ref.dst}`);
  }

  // through the worker's messages: progress reports, then the answer, or nothing once cancelled
  const got = [];
  w.onmessage = (e) => got.push(e.data);
  const msg = { type: "connect", src: slow.src, dst: slow.dst, allowed: allowedOf(slow), avoid: slow.avoid, k: 3, pauseMs: 0 };
  w.postMessage({ ...msg, id: 1 });
  w.postMessage({ ...msg, id: 2 });
  w.postMessage({ type: "cancel", id: 2 });
  while (got.filter((m) => m.type === "result").length < 2) await new Promise((r) => setTimeout(r, 5));
  const r1 = got.find((m) => m.type === "result" && m.id === 1), r2 = got.find((m) => m.type === "result" && m.id === 2);
  check("worker answer", r1.result && r1.result.degrees === slow.degrees && r1.result.visited === slow.visited);
  check("worker progress", got.some((m) => m.type === "searching" && m.id === 1));
  check("worker cancel", r2.cancelled === true && !got.some((m) => m.type === "searching" && m.id === 2));
}

/* ---------------------------------------------------------- 2. engine vs the Python API */

async function testEngine(app) {
  const ctx = vm.createContext({ ...shared, fetch: fileFetch, Worker: FakeWorker, structuredClone, queueMicrotask });
  ctx.window = ctx;
  ctx.location = { href: pathToFileURL(site + "/").href };
  ctx.SIXDEG_STATIC = { data: "data/", version: meta.version };
  vm.runInContext(fs.readFileSync(path.join(WEB, "engine.js"), "utf8"), ctx, { filename: "engine.js" });
  const api = ctx.SixDeg;

  let same = 0;
  for (const { q, ids } of app.search) {
    const got = (await api.call("/api/search", { q, limit: 8 })).map((p) => p.id);
    const ok = JSON.stringify(got) === JSON.stringify(ids);
    same += ok;
    if (!ok) console.log(`  search "${q}": static ${JSON.stringify(got)} vs python ${JSON.stringify(ids)}`);
  }
  console.log(`name search: ${same} of ${app.search.length} queries give the same top 8 as SQLite FTS`);
  check("name search agreement", same >= app.search.length * 0.85, `${same}/${app.search.length}`);

  // a stopped search rejects with an AbortError, and leaves the next one unaffected
  const ctl = new AbortController(), first = app.connect[0];
  const stopped = api.call("/api/connect", { from: first.from, to: first.to, k: 4 }, { signal: ctl.signal });
  ctl.abort();
  const err = await stopped.then(() => null, (e) => e);
  check("abort", err && err.name === "AbortError", String(err));

  for (const ref of app.connect) {
    const r = await api.call("/api/connect", { from: ref.from, to: ref.to, k: 4 });
    check("connect degrees", r.degrees === ref.degrees, `${ref.from}->${ref.to}: ${r.degrees} vs ${ref.degrees}`);
    const mine = r.paths[0] ? r.paths[0].links.map((l) => l.sentence) : [];
    if (JSON.stringify(mine) !== JSON.stringify(ref.sentences)) {
      console.log(`  ${r.from.name} → ${r.to.name}:\n    static: ${mine.join(" | ")}\n    python: ${ref.sentences.join(" | ")}`);
    } else {
      console.log(`  ${r.from.name} → ${r.to.name}: ${r.degrees} — same sentences`);
    }
  }

  for (const ref of app.person) {
    const d = await api.call("/api/person", { id: ref.qid });
    const mine = canon(summarize(d)), theirs = canon(ref.summary);
    const same = JSON.stringify(mine) === JSON.stringify(theirs);
    // equally well-known entries may come in a different order (ties are broken differently)
    const ok = same || JSON.stringify(unordered(mine)) === JSON.stringify(unordered(theirs));
    console.log(`  panel ${d.person.name}: ${same ? "same" : ok ? "same entries, ties ordered differently" : "DIFFERENT"}`);
    if (!same) {
      for (const part of ["rel", "ctx"]) {
        for (const k of new Set([...Object.keys(mine[part]), ...Object.keys(theirs[part])])) {
          const a = JSON.stringify(mine[part][k]), b = JSON.stringify(theirs[part][k]);
          if (a !== b) console.log(`    ${part}.${k}\n      static: ${a}\n      python: ${b}`);
        }
      }
    }
    check("person panel", ok, ref.qid);
  }
  const s = await api.call("/api/stats");
  check("stats", s.people === meta.stats.people);
  console.log(`${ranges} range reads`);
}

// objects with their keys sorted, so key order doesn't count as a difference
const canon = (x) => (Array.isArray(x) ? x.map(canon) : x && typeof x === "object"
  ? Object.fromEntries(Object.keys(x).sort().map((k) => [k, canon(x[k])])) : x);

const unordered = (summary) => ({
  rel: summary.rel,
  ctx: Object.fromEntries(Object.entries(summary.ctx).map(([k, [n, items]]) => [k, [n, items.map((x) => JSON.stringify(x)).sort()]])),
});

// the parts of a person panel both versions should agree on
function summarize(d) {
  const rel = Object.fromEntries(Object.entries(d.relations).map(([k, v]) => [k, [v.total ?? v.length, v.slice(0, 25).map((x) => x.sentence)]]));
  const ctx = Object.fromEntries(Object.entries(d.contexts).map(([k, v]) => [k, [v.total, v.items.map((x) => [x.title, x.role, x.size, x.seasons, x.with.map((m) => m.qid)])]]));
  return { rel, ctx };
}

if (refsPath) await testGraph(JSON.parse(fs.readFileSync(refsPath, "utf8")));
if (appPath) await testEngine(JSON.parse(fs.readFileSync(appPath, "utf8")));
if (failures.length) {
  console.log(`\n${failures.length} failures:\n  ` + failures.slice(0, 40).join("\n  "));
  process.exit(1);
}
console.log("ok");
process.exit(0);
