"""MusicBrainz (CC0): musicians, the relationships between them, and who worked on which
recordings, songs and albums, from MusicBrainz's monthly JSON dump of artists.

fetch() downloads artist.tar.xz (~2 GB) and writes four CSVs to raw/:
  mb_artists   mbid, type, name, gender, begin, end, disambiguation, country, wikidata
  mb_aliases   mbid, alias                                  (people only, for name search)
  mb_links     a, b, type, begin, end, attributes           (artist <-> artist)
  mb_credits   kind, target, title, date, artist, role      (artist -> recording / work / release)

Each relationship is listed under both of its artists; only the "forward" copy is kept.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import tarfile
import time
import urllib.request

DUMPS = "https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/"
USER_AGENT = "sixdeg/1.0 (six-degrees people graph builder; personal project)"

# Credits that mean people worked on the thing together. Left out: remixes and DJ mixes (often
# made years later without the original artists), mastering, rights holders, and artwork/notes.
CREDIT_ROLES = {
    "recording": {"performer", "instrument", "vocal", "conductor", "chorus master", "concertmaster",
                  "producer", "arranger", "orchestrator", "programming", "engineer", "recording", "mix",
                  "sound", "audio", "balance", "instrument arranger", "vocal arranger"},
    "release": {"performer", "instrument", "vocal", "conductor", "chorus master", "concertmaster",
                "producer", "arranger", "orchestrator", "programming", "engineer", "recording", "mix"},
    "work": {"composer", "lyricist", "writer", "librettist"},
}
ARTIST_LINKS = {"member of band", "collaboration", "founder", "conductor position", "married",
                "involved with", "sibling", "parent", "teacher", "supporting musician",
                "vocal supporting musician", "instrumental supporting musician"}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def _get(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def download(raw_dir: str, refresh: bool = False) -> str:
    """The latest artist dump, kept in raw/musicbrainz/ (re-downloaded when a newer one exists)."""
    folder = os.path.join(raw_dir, "musicbrainz")
    os.makedirs(folder, exist_ok=True)
    with urllib.request.urlopen(_get(DUMPS + "LATEST"), timeout=60) as r:
        latest = r.read().decode().strip()
    path = os.path.join(folder, "artist.tar.xz")
    stamp = os.path.join(folder, "VERSION")
    have = open(stamp).read().strip() if os.path.exists(stamp) and os.path.exists(path) else None
    if have == latest and not refresh:
        return path
    log(f"  downloading MusicBrainz artist dump {latest} (~2 GB)")
    with urllib.request.urlopen(_get(f"{DUMPS}{latest}/artist.tar.xz"), timeout=600) as r, \
            open(path + ".part", "wb") as out:
        while chunk := r.read(1 << 22):
            out.write(chunk)
    os.replace(path + ".part", path)
    with open(stamp, "w") as f:
        f.write(latest)
    return path


def _year(date) -> str:
    return (date or "")[:4] if (date or "")[:4].isdigit() else ""


def parse(tar_path: str, raw_dir: str) -> dict:
    """Stream the dump into the four CSVs. Returns counts."""
    names = ("mb_artists", "mb_aliases", "mb_links", "mb_credits")
    files = {n: gzip.open(os.path.join(raw_dir, n + ".csv.gz.part"), "wt", encoding="utf-8", newline="",
                          compresslevel=5) for n in names}
    out = {n: csv.writer(f) for n, f in files.items()}
    out["mb_artists"].writerow(["mbid", "type", "name", "gender", "begin", "end", "disambiguation", "country",
                                "wikidata"])
    out["mb_aliases"].writerow(["mbid", "alias"])
    out["mb_links"].writerow(["a", "b", "type", "begin", "end", "attributes"])
    out["mb_credits"].writerow(["kind", "target", "title", "date", "artist", "role"])
    counts = dict.fromkeys(names, 0)
    with tarfile.open(tar_path, "r|xz") as tar:
        for member in tar:
            if not member.name.endswith("mbdump/artist"):
                continue
            for line in tar.extractfile(member):  # (a TextIOWrapper needs seeking, which a stream lacks)
                a = json.loads(line)
                mbid, life = a["id"], a.get("life-span") or {}
                wikidata = sorted({r["url"]["resource"].rsplit("/", 1)[-1] for r in a.get("relations", ())
                                   if r.get("target-type") == "url" and r["type"] == "wikidata"
                                   and "/wiki/Q" in r["url"]["resource"]})
                out["mb_artists"].writerow([mbid, a.get("type") or "", a["name"], a.get("gender") or "",
                                            life.get("begin") or "", life.get("end") or "",
                                            a.get("disambiguation") or "", a.get("country") or "",
                                            "|".join(wikidata)])
                counts["mb_artists"] += 1
                if a.get("type") == "Person":
                    for al in {x["name"] for x in a.get("aliases") or () if x.get("name") and x["name"] != a["name"]}:
                        out["mb_aliases"].writerow([mbid, al])
                        counts["mb_aliases"] += 1
                for r in a.get("relations") or ():
                    tt = r.get("target-type")
                    if r.get("direction") != "forward":
                        continue
                    if tt == "artist":
                        if r["type"] in ARTIST_LINKS:
                            out["mb_links"].writerow([mbid, r["artist"]["id"], r["type"], r.get("begin") or "",
                                                      r.get("end") or "", "|".join(r.get("attributes") or ())])
                            counts["mb_links"] += 1
                    elif tt in CREDIT_ROLES and r["type"] in CREDIT_ROLES[tt]:
                        t = r[tt]
                        date = t.get("date") or t.get("first-release-date") or ""
                        out["mb_credits"].writerow([tt, t["id"], t.get("title") or "", _year(date), mbid, r["type"]])
                        counts["mb_credits"] += 1
            break
    for n, f in files.items():
        f.close()
        os.replace(os.path.join(raw_dir, n + ".csv.gz.part"), os.path.join(raw_dir, n + ".csv.gz"))
    return counts


def fetch(raw_dir: str, refresh: bool = False) -> None:
    t0 = time.time()
    path = download(raw_dir, refresh)
    if not refresh and all(os.path.exists(os.path.join(raw_dir, n + ".csv.gz")) and
                           os.path.getmtime(os.path.join(raw_dir, n + ".csv.gz")) >= os.path.getmtime(path)
                           for n in ("mb_artists", "mb_aliases", "mb_links", "mb_credits")):
        return
    log("  reading the MusicBrainz artist dump")
    counts = parse(path, raw_dir)
    log("  MusicBrainz: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()) + f" ({time.time() - t0:.0f}s)")
