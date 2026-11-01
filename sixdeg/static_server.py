"""Local preview of the exported static site. Unlike `python3 -m http.server`, this answers HTTP
range requests, which the page uses to read single shards out of the big data files (as it does
on GitHub Pages)."""
from __future__ import annotations

import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: the page makes many small requests
    remaining = None

    def log_message(self, fmt, *args):  # shard reads are noise
        if "/data/" not in self.path:
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        if "/data/" not in self.path:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        self.remaining = None
        path = self.translate_path(self.path)
        m = _RANGE.match((self.headers.get("Range") or "").strip())
        if not m or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        first, last = m.groups()
        if first:
            start, end = int(first), min(int(last), size - 1) if last else size - 1
        else:
            start, end = max(0, size - int(last or 0)), size - 1
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        if self.remaining is None:
            return super().copyfile(source, outputfile)
        while self.remaining > 0:
            chunk = source.read(min(1 << 16, self.remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            self.remaining -= len(chunk)


def serve_static(site_dir: str, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = False) -> None:
    if not os.path.isfile(os.path.join(site_dir, "index.html")):
        raise SystemExit(f"no site in {site_dir}: run `python3 sixdeg.py site` first")
    httpd = ThreadingHTTPServer((host, port), partial(RangeHandler, directory=site_dir))
    print(f"Static Six Degrees at http://{host}:{port}  (serving {site_dir})", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
