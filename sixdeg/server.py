"""Local web app: JSON API + static UI. Stdlib only (http.server)."""
from __future__ import annotations

import json
import mimetypes
import os
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from . import CATEGORIES, CATEGORY_CODE, DEFAULT_OFF
from .graph import Graph
from .store import Store

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class App:
    def __init__(self, data_dir: str):
        t0 = time.time()
        self.store = Store(os.path.join(data_dir, "people.db"))
        self.graph = Graph(os.path.join(data_dir, "graph"))
        self.load_seconds = round(time.time() - t0, 1)

    @staticmethod
    def parse_categories(value):
        keys = [k for k, _ in CATEGORIES]
        if value:
            chosen = {c for c in value.split(",") if c in CATEGORY_CODE}
        else:
            chosen = set(keys) - DEFAULT_OFF
        mask = np.array([k in chosen for k in keys], bool)
        return chosen, mask

    def connect(self, a_text, b_text, cats_value=None, avoid_value="", k=3, max_degrees=12):
        A = self.store.person_by_qid(a_text) or self.store.resolve(a_text)
        B = self.store.person_by_qid(b_text) or self.store.resolve(b_text)
        if not A or not B:
            missing = a_text if not A else b_text
            return {"error": f"No person found for “{missing}”."}
        cats, mask = self.parse_categories(cats_value)
        avoid = []
        for q in filter(None, (avoid_value or "").split(",")):
            p = self.store.person_by_qid(q)
            if p:
                avoid.append(p)
        res = self.graph.connect(A["id"], B["id"], mask, [p["id"] for p in avoid], k=k, max_degrees=max_degrees)
        out = {"from": A, "to": B, "degrees": res["degrees"], "ms": res["ms"], "visited": res["visited"],
               "categories": sorted(cats), "avoid": avoid, "paths": []}
        if res["degrees"] is None:
            lonely = [p["name"] for p in (A, B) if p["n_relations"] + p["n_contexts"] == 0]
            out["reason"] = (f"{' and '.join(lonely)} {'has' if len(lonely) == 1 else 'have'} no recorded "
                             "connections in the database." if lonely else
                             f"No route within {max_degrees} degrees using the selected link types.")
            return out
        ids = {n for people, _ in res["paths"] for n in people}
        cards = self.store.people(ids)
        for people, links in res["paths"]:
            out["paths"].append({
                "people": [cards[n] for n in people],
                "links": self.store.explain(people, links, cats, cards),
            })
        return out


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SixDegrees/1.0"

        def log_message(self, fmt, *args):  # quieter console
            if not self.path.startswith("/api/search"):
                super().log_message(fmt, *args)

        def send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200 if urllib.parse.urlparse(self.path).path in ("/", "/index.html") else 404)
            self.end_headers()

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                if url.path == "/api/search":
                    return self.send_json(app.store.search(q.get("q", ""), min(int(q.get("limit", 8)), 25)))
                if url.path == "/api/connect":
                    return self.send_json(app.connect(q.get("from", ""), q.get("to", ""), q.get("cats"),
                                                      q.get("avoid", ""), k=min(int(q.get("k", 4)), 10)))
                if url.path == "/api/person":
                    p = app.store.person_by_qid(q.get("id", "")) or app.store.resolve(q.get("id", ""))
                    if not p:
                        return self.send_json({"error": "not found"}, 404)
                    return self.send_json(app.store.neighborhood(p["id"]))
                if url.path == "/api/random":
                    return self.send_json(app.store.random_pair())
                if url.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if url.path == "/api/stats":
                    s = app.store.stats()
                    s["categories"] = [{"key": k, "label": lbl, "default": k not in DEFAULT_OFF}
                                       for k, lbl in CATEGORIES]
                    return self.send_json(s)
                return self.serve_static(url.path)
            except BrokenPipeError:
                pass
            except Exception as e:  # report, keep serving
                traceback.print_exc()
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        def serve_static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB_DIR, name))
            if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
                return self.send_json({"error": "not found"}, 404)
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(full)[0] or "application/octet-stream")
                             + ("; charset=utf-8" if full.endswith((".html", ".js", ".css")) else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(data_dir: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    print("loading people database and graph ...", flush=True)
    app = App(data_dir)
    s = app.store.stats()
    print(f"{s['people']:,} people, {app.graph.meta['direct_edges']:,} direct links, "
          f"{s['contexts']:,} shared contexts (loaded in {app.load_seconds}s)", flush=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Six Degrees running at http://{host}:{port}", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
