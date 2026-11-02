"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n).toLocaleString("en-US");

const state = {
  from: null,
  to: null,
  cats: new Set(),
  categories: [],     // [{key, label, default}]
  avoid: [],          // person cards
  result: null,
  route: 0,
};

// On a static host (see `sixdeg.py site`) engine.js answers these in the browser.
const engine = window.SixDeg || null;

// opts: { signal, onProgress } -- a search can be stopped; only the static engine reports progress
async function api(path, params = {}, opts = {}) {
  if (engine) return engine.call(path, params, opts);
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  const res = await fetch(url, { signal: opts.signal });
  const body = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok || body.error) throw new Error(body.error || res.statusText);
  return body;
}

function initials(name) {
  const parts = String(name || "?").replace(/\(.*?\)/g, "").trim().split(/\s+/);
  return ((parts[0] || "?")[0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

function avatar(p, size = "") {
  const img = p.image_url ? `<img src="${esc(p.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : "";
  return `<span class="av ${size}" aria-hidden="true">${esc(initials(p.name))}${img}</span>`;
}

function metaLine(p) {
  return [p.years, p.description].filter(Boolean).join(" · ");
}

/* ------------------------------------------------------------ person pickers */

function makePicker(root, onChange) {
  const input = $("input", root);
  const list = $(".suggest", root);
  const metaEl = $(".chosen-meta", root);
  const avatarEl = $(".field-avatar", root);
  let items = [];
  let active = -1;
  let timer = null;
  let seq = 0;
  let current = null;

  function open() { list.hidden = false; input.setAttribute("aria-expanded", "true"); }
  function close() { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; }

  function render() {
    if (!items.length) {
      list.innerHTML = `<li class="empty">No one by that name in the database</li>`;
    } else {
      list.innerHTML = items.map((p, i) => `
        <li role="option" id="${list.id}-${i}" data-i="${i}" aria-selected="${i === active}">
          ${avatar(p)}<span class="txt"><b>${esc(p.name)}</b><small>${esc(metaLine(p) || p.qid)}</small></span>
        </li>`).join("");
    }
    open();
  }

  function setActive(i) {
    active = i;
    [...list.children].forEach((li, j) => li.setAttribute("aria-selected", String(j === i)));
    if (i >= 0) {
      input.setAttribute("aria-activedescendant", `${list.id}-${i}`);
      list.children[i]?.scrollIntoView({ block: "nearest" });
    }
  }

  async function search() {
    const q = input.value.trim();
    if (!q) { items = []; close(); return; }
    const mine = ++seq;
    try {
      const res = await api("/api/search", { q, limit: 8 });
      if (mine !== seq) return;
      items = res;
      active = items.length ? 0 : -1;
      render();
    } catch (e) { /* keep typing */ }
  }

  function set(p, notify = true) {
    current = p;
    input.value = p ? p.name : "";
    root.classList.toggle("picked", !!p);
    metaEl.textContent = p ? metaLine(p) : "";
    avatarEl.innerHTML = p ? avatar(p, "sm") : "";
    close();
    if (notify) onChange(p);
  }

  input.addEventListener("input", () => {
    if (current) { current = null; root.classList.remove("picked"); metaEl.textContent = ""; onChange(null); }
    clearTimeout(timer);
    timer = setTimeout(search, 110);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && items.length) { e.preventDefault(); if (list.hidden) open(); setActive(Math.min(active + 1, items.length - 1)); }
    else if (e.key === "ArrowUp" && items.length) { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (!list.hidden && items[active]) set(items[active]);
      else if (current) go();
    } else if (e.key === "Escape") close();
  });
  input.addEventListener("focus", () => { if (!current && items.length) render(); });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); set(items[Number(li.dataset.i)]); }
  });
  document.addEventListener("click", (e) => { if (!root.contains(e.target)) close(); });

  return { set, get: () => current, focus: () => input.focus() };
}

const fromPicker = makePicker($("#pick-from"), (p) => { state.from = p; update(); });
const toPicker = makePicker($("#pick-to"), (p) => { state.to = p; update(); });

function update() {
  $("#go").disabled = !(state.from && state.to);
  const box = $("#avoiding");
  box.hidden = !state.avoid.length;
  box.innerHTML = state.avoid.length
    ? "Avoiding " + state.avoid.map((p) => `<span class="tag">${esc(p.name)}<button type="button" data-unavoid="${esc(p.qid)}" aria-label="Stop avoiding ${esc(p.name)}">×</button></span>`).join(" ")
    : "";
}

/* ------------------------------------------------------------ categories */

function renderCats() {
  $("#cats").innerHTML = state.categories.map((c) => `
    <button type="button" class="chip cat-${c.key}" data-cat="${c.key}" aria-pressed="${state.cats.has(c.key)}">
      <span class="dot"></span>${esc(c.label)}
    </button>`).join("");
}

$("#cats").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  const k = chip.dataset.cat;
  if (state.cats.has(k)) state.cats.delete(k); else state.cats.add(k);
  if (!state.cats.size) state.cats.add(k);  // keep at least one
  renderCats();
  if (state.result && state.from && state.to) go();
});

/* ------------------------------------------------------------ search */

let searchSeq = 0;
let running = null;  // AbortController of the search in progress

const degreesText = (n) => `${n} degree${n === 1 ? "" : "s"}`;

// what a long search has done so far ({degrees, visited}), or that it is done ({found})
function progressText(p) {
  if (p.found != null) return `Found a connection: ${degreesText(p.found)}. Writing it up…`;
  return `Still searching: no connection within ${degreesText(p.degrees)} so far · ${fmt(p.visited)} people checked`;
}

// Static site: the network the search runs on is downloaded once, then cached by the browser.
function networkText(n) {
  if (n.error) return n.error;
  if (n.decoding) return "Unpacking the network…";
  const mb = (x) => (x / 1e6).toFixed(0);
  return n.total ? `Downloading the network: ${mb(n.loaded)} of ${mb(n.total)} MB` : "Downloading the network…";
}

if (engine) {
  engine.onNetwork((n) => {
    const el = $("#net");
    el.hidden = n.ready && !n.error;
    el.classList.toggle("err", !!n.error);
    el.innerHTML = n.error ? esc(n.error)
      : `<span class="bar"><i style="width:${n.total ? Math.round((100 * n.loaded) / n.total) : 0}%"></i></span>${esc(networkText(n))}`;
  });
}

// There's no limit on the degrees: the search runs until it finds the shortest route or has
// checked everyone either person can reach, and the Stop button ends it early.
async function go() {
  if (!(state.from && state.to)) return;
  if (running) running.abort();  // a new search replaces one still going
  const ctl = (running = new AbortController());
  const mine = ++searchSeq;
  writeHash();
  const out = $("#result");
  out.innerHTML = `<div class="notice loading"><span class="spinner"></span>
    <span class="msg" aria-live="off">Searching for the shortest connection…</span>
    <button type="button" class="mini-btn stop">Stop</button></div>`;
  const msg = $(".msg", out), stopBtn = $(".stop", out);
  stopBtn.addEventListener("click", () => ctl.abort());
  let last = null;  // the latest progress report
  const unwatch = engine && !engine.network.ready
    ? engine.onNetwork((n) => {
      if (!n.ready && !n.error) msg.textContent = `${networkText(n)} — only the first search waits for this…`;
    })
    : null;
  try {
    const res = await api("/api/connect", {
      from: state.from.qid, to: state.to.qid, k: 6,
      cats: [...state.cats].join(","), avoid: state.avoid.map((p) => p.qid).join(","),
    }, {
      signal: ctl.signal,
      onProgress: (p) => {
        last = p;
        msg.textContent = progressText(p);
        if (p.found != null) stopBtn.hidden = true;  // only the write-up is left
      },
    });
    if (mine !== searchSeq) return;
    state.result = res;
    state.route = 0;
    renderResult();
  } catch (e) {
    if (mine !== searchSeq) return;
    out.innerHTML = e.name !== "AbortError" ? `<div class="notice err">${esc(e.message)}</div>`
      : `<div class="notice"><strong>Search stopped.</strong>
          ${last ? `There's no connection within ${degreesText(last.degrees)} (${fmt(last.visited)} people checked).` : ""}</div>`;
  } finally {
    if (unwatch) unwatch();
    if (running === ctl) running = null;
  }
}

// Bold each person's first mention only (a name can recur inside a title: "Demi Lovato: Stay Strong").
function boldNames(sentence, names) {
  const text = esc(sentence);
  const marks = [];
  for (const n of [...new Set(names)].filter(Boolean).sort((a, b) => b.length - a.length)) {
    const needle = esc(n);
    let at = text.indexOf(needle);
    while (at >= 0 && marks.some(([s, e]) => at < e && at + needle.length > s)) at = text.indexOf(needle, at + 1);
    if (at >= 0) marks.push([at, at + needle.length]);
  }
  marks.sort((x, y) => x[0] - y[0]);
  let html = "", pos = 0;
  for (const [s, e] of marks) {
    html += text.slice(pos, s) + `<strong>${text.slice(s, e)}</strong>`;
    pos = e;
  }
  return html + text.slice(pos);
}

function renderResult() {
  const res = state.result;
  const out = $("#result");
  if (!res) { out.innerHTML = ""; return; }
  if (res.degrees === null) {
    const tries = [];
    if (state.cats.size < state.categories.length) tries.push("switching on more link types");
    if (state.avoid.length) tries.push(`no longer avoiding ${state.avoid.map((p) => p.name).join(" and ")}`);
    out.innerHTML = `<div class="notice"><strong>No connection found.</strong> ${esc(res.reason || "")}
      ${tries.length ? esc(` Try ${tries.join(" or ")}.`) : ""}</div>`;
    return;
  }
  if (res.degrees === 0) {
    out.innerHTML = `<div class="notice">That's the same person twice — zero degrees of separation.</div>`;
    return;
  }
  const path = res.paths[state.route];
  const n = res.degrees;
  let html = `
    <div class="headline"><span class="num">${n}</span><span class="what">degree${n === 1 ? "" : "s"} of separation</span></div>
    <p class="subline">${esc(res.from.name)} → ${esc(res.to.name)} · searched ${fmt(res.visited)} people in ${fmt(res.ms)} ms
      ${res.paths.length > 1 ? ` · ${res.paths.length} equally short routes shown` : ""}</p>
    <ol class="chain">`;
  path.people.forEach((p, i) => {
    const inner = i > 0 && i < path.people.length - 1;
    html += `
      <li class="node">
        ${avatar(p, "lg")}
        <div class="who">
          <a class="name" href="${esc(p.wikipedia || p.wikidata || p.link)}" target="_blank" rel="noopener">${esc(p.name)}</a>
          <div class="meta">${esc(metaLine(p))}</div>
        </div>
        <div class="btns">
          <button type="button" class="mini-btn" data-explore="${esc(p.qid)}">Connections</button>
          ${inner ? `<button type="button" class="mini-btn" data-avoid="${esc(p.qid)}" title="Find a route that doesn't go through ${esc(p.name)}">Avoid</button>` : ""}
        </div>
      </li>`;
    const link = path.links[i];
    if (link) {
      const also = link.also && link.also.count
        ? `<p class="also">Also in common: ${esc(link.also.items.join(" · "))}${link.also.count > link.also.items.length ? ` · +${link.also.count - link.also.items.length} more` : ""}</p>`
        : "";
      // where this link comes from, linked to the record when there is one page for it
      const from = link.sources && link.sources.length ? `Source: ${esc(link.sources.join(" · "))}` : "";
      const src = from && `<p class="src">${link.url ? `<a href="${esc(link.url)}" target="_blank" rel="noopener">${from} ↗</a>` : from}</p>`;
      html += `
        <li class="edge cat-${link.category}">
          <span class="pill">${esc(link.category_label)}</span>
          <p class="sentence">${boldNames(link.sentence, [p.name, path.people[i + 1].name])}</p>
          ${also}${src}
        </li>`;
    }
  });
  html += `</ol>`;
  if (res.paths.length > 1) {
    html += `<div class="routes"><h3>Other routes of the same length</h3>` +
      res.paths.map((r, j) => `
        <button type="button" class="route" data-route="${j}" aria-current="${j === state.route}">
          ${r.people.map((p) => `<span>${esc(p.name)}</span>`).join(`<span class="arrow">→</span>`)}
        </button>`).join("") + `</div>`;
  }
  out.innerHTML = html;
}

$("#result").addEventListener("click", (e) => {
  const ex = e.target.closest("[data-explore]");
  if (ex) return explore(ex.dataset.explore);
  const av = e.target.closest("[data-avoid]");
  if (av) {
    const p = state.result.paths[state.route].people.find((x) => x.qid === av.dataset.avoid);
    if (p && !state.avoid.some((x) => x.qid === p.qid)) state.avoid.push(p);
    update();
    return go();
  }
  const r = e.target.closest("[data-route]");
  if (r) {
    state.route = Number(r.dataset.route);
    renderResult();
    $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

$("#avoiding").addEventListener("click", (e) => {
  const b = e.target.closest("[data-unavoid]");
  if (!b) return;
  state.avoid = state.avoid.filter((p) => p.qid !== b.dataset.unavoid);
  update();
  go();
});

$("#go").addEventListener("click", go);
$("#swap").addEventListener("click", () => {
  const a = state.from, b = state.to;
  fromPicker.set(b);
  toPicker.set(a);
  if (state.result && a && b) go();
});
$("#random").addEventListener("click", async () => {
  try {
    const [a, b] = await api("/api/random");
    state.avoid = [];
    fromPicker.set(a);
    toPicker.set(b);
    go();
  } catch (e) {
    $("#result").innerHTML = `<div class="notice err">${esc(e.message)}</div>`;
  }
});

/* ------------------------------------------------------------ person drawer */

async function explore(qid) {
  const drawer = $("#drawer");
  const body = $("#drawer-body");
  drawer.hidden = false;
  $("#scrim").hidden = false;
  body.innerHTML = `<button class="close" type="button" aria-label="Close">×</button><div class="notice loading"><span class="spinner"></span>Loading connections…</div>`;
  try {
    const d = await api("/api/person", { id: qid });
    const p = d.person;
    let html = `
      <button class="close" type="button" aria-label="Close">×</button>
      <div class="head">${avatar(p, "xl")}<div><h2>${esc(p.name)}</h2><div class="meta">${esc(metaLine(p))}</div></div></div>
      <div class="links">
        ${p.wikipedia ? `<a href="${esc(p.wikipedia)}" target="_blank" rel="noopener">Wikipedia ↗</a>` : ""}
        <a href="${esc(p.wikidata || p.link)}" target="_blank" rel="noopener">${esc(p.wikidata ? "Wikidata" : p.source)} ↗</a>
        <button type="button" class="mini-btn" data-set-from="${esc(p.qid)}">Use as “From”</button>
        <button type="button" class="mini-btn" data-set-to="${esc(p.qid)}">Use as “To”</button>
      </div>`;
    drawer._person = p;
    const cats = state.categories.map((c) => c.key);
    const sections = [];
    for (const k of cats) {
      const rels = d.relations[k];
      const ctx = d.contexts[k];
      if (!rels && !ctx) continue;
      let s = `<section class="cat-${k}"><h3>${esc(d.labels[k])}</h3><ul>`;
      for (const r of (rels || []).slice(0, 25)) {
        s += `<li>${boldNames(r.sentence, [r.person.name, p.name]).replace(
          `<strong>${esc(r.person.name)}</strong>`,
          `<button type="button" class="linkish" data-explore="${esc(r.person.qid)}">${esc(r.person.name)}</button>`)}</li>`;
      }
      const nRels = rels ? rels.total ?? rels.length : 0;  // the static engine spells out only 25
      if (nRels > 25) s += `<li class="more">… and ${nRels - 25} more</li>`;
      for (const c of (ctx ? ctx.items : [])) {
        s += `<li><div class="ctx-title">${esc(c.title)}</div>
          <div class="ctx-role">${esc(roleText(c.role))} · ${c.seasons > 1 ? `${c.seasons} seasons` : `${c.size} people`}</div>
          ${c.with.length ? `<div class="with">with ${c.with.map((m) => `<button type="button" class="linkish" data-explore="${esc(m.qid)}">${esc(m.name)}</button>`).join("")}</div>` : ""}
        </li>`;
      }
      if (ctx && ctx.total > ctx.items.length) s += `<li class="more">… and ${ctx.total - ctx.items.length} more</li>`;
      sections.push(s + `</ul></section>`);
    }
    html += sections.join("") || `<p class="more">No recorded connections.</p>`;
    body.innerHTML = html;
    body.scrollTop = 0;
    drawer.scrollTop = 0;
  } catch (e) {
    body.innerHTML = `<button class="close" type="button" aria-label="Close">×</button><div class="notice err">${esc(e.message)}</div>`;
  }
}

// MusicBrainz credit names, as people would say them
const ROLE_TEXT = { vocal: "vocals", instrument: "musician", recording: "recording engineer", mix: "mixing engineer",
                    programming: "programmer", writer: "songwriter" };
const roleText = (roles) => (roles || "member").split(", ").map((r) => ROLE_TEXT[r] || r).join(", ");

function closeDrawer() { $("#drawer").hidden = true; $("#scrim").hidden = true; }
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer(); });
$("#drawer").addEventListener("click", (e) => {
  if (e.target.closest(".close")) return closeDrawer();
  const ex = e.target.closest("[data-explore]");
  if (ex) return explore(ex.dataset.explore);
  const p = $("#drawer")._person;
  if (e.target.closest("[data-set-from]")) { fromPicker.set(p); closeDrawer(); if (state.to) go(); }
  if (e.target.closest("[data-set-to]")) { toPicker.set(p); closeDrawer(); if (state.from) go(); }
});

/* ------------------------------------------------------------ URL state */

function writeHash() {
  const h = new URLSearchParams();
  if (state.from) h.set("from", state.from.qid);
  if (state.to) h.set("to", state.to.qid);
  const defaults = state.categories.filter((c) => c.default).map((c) => c.key).sort().join(",");
  const cats = [...state.cats].sort().join(",");
  if (cats !== defaults) h.set("cats", cats);
  if (state.avoid.length) h.set("avoid", state.avoid.map((p) => p.qid).join(","));
  history.replaceState(null, "", "#" + h.toString());
}

async function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("cats")) {
    const keys = new Set(state.categories.map((c) => c.key));
    const chosen = h.get("cats").split(",").filter((k) => keys.has(k));
    if (chosen.length) state.cats = new Set(chosen);
  }
  const card = async (qid) => (qid ? (await api("/api/search", { q: qid, limit: 1 }))[0] : null);
  const [a, b] = await Promise.all([card(h.get("from")), card(h.get("to"))]);
  const avoid = await Promise.all((h.get("avoid") || "").split(",").filter(Boolean).map(card));
  state.avoid = avoid.filter(Boolean);
  if (a) fromPicker.set(a);
  if (b) toPicker.set(b);
  renderCats();
  update();
  if (a && b) go();
}

window.addEventListener("hashchange", () => {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("from") !== state.from?.qid || h.get("to") !== state.to?.qid) readHash();
});

/* ------------------------------------------------------------ boot */

(async function boot() {
  try {
    const s = await api("/api/stats");
    state.categories = s.categories;
    state.cats = new Set(s.categories.filter((c) => c.default).map((c) => c.key));
    $("#stats").textContent = `${fmt(s.people)} people · ${fmt(s.relations)} direct relationships · ${fmt(s.contexts)} films, teams, bands & more · built ${s.built_at}`;
    renderCats();
    await readHash();
    if (!state.from) fromPicker.focus();
  } catch (e) {
    $("#stats").textContent = "Could not load the database: " + e.message;
  }
})();
