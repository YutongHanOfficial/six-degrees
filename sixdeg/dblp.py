"""dblp (CC0): computer-science publications and their authors, from the monthly dblp.xml dump.

dblp gives every author a person record (homepages/...) and a unique name string (namesakes get a
number: "Wei Wang 0001"), so authors are matched by dblp's own ids, never by guessing from names.

fetch() downloads dblp.xml.gz (~1.1 GB) and dblp.dtd and writes four CSVs to raw/:
  dblp_people    pid, name, affiliation, papers, wikidata, orcid
  dblp_names     name, pid                              (every name string dblp uses for a person)
  dblp_papers    key, title, year                       (papers with 2-25 authors)
  dblp_authors   key, name                              (dblp's unique author name; see dblp_names)
"""
from __future__ import annotations

import csv
import gzip
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

XML_URL = "https://dblp.org/xml/dblp.xml.gz"
DTD_URL = "https://dblp.org/xml/dblp.dtd"
USER_AGENT = "sixdeg/1.0 (six-degrees people graph builder; personal project)"
PAPERS = {"article", "inproceedings", "incollection", "book"}   # (proceedings list editors, theses one author)
MAX_AUTHORS = 25
_SUFFIX = re.compile(r" \d{4}$")   # "Wei Wang 0001" -> "Wei Wang"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def download(raw_dir: str, refresh: bool = False) -> str:
    folder = os.path.join(raw_dir, "dblp")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "dblp.xml.gz")
    for url, dest in ((XML_URL, path), (DTD_URL, os.path.join(folder, "dblp.dtd"))):
        if os.path.exists(dest) and not refresh:
            continue
        log(f"  downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=600) as r, open(dest + ".part", "wb") as out:
            while chunk := r.read(1 << 22):
                out.write(chunk)
        os.replace(dest + ".part", dest)
    return path


def display_name(name: str) -> str:
    return _SUFFIX.sub("", name)


def parse(xml_path: str, raw_dir: str) -> dict:
    with open(os.path.join(os.path.dirname(xml_path), "dblp.dtd"), encoding="latin-1") as f:
        entities = {m[0]: chr(int(m[1])) for m in re.findall(r'<!ENTITY\s+(\w+)\s+"&#(\d+);"', f.read())}
    parser = ET.XMLParser()
    parser.entity.update(entities)
    names = ("dblp_people", "dblp_names", "dblp_papers", "dblp_authors")
    files = {n: gzip.open(os.path.join(raw_dir, n + ".csv.gz.part"), "wt", encoding="utf-8", newline="",
                          compresslevel=5) for n in names}
    out = {n: csv.writer(f) for n, f in files.items()}
    out["dblp_papers"].writerow(["key", "title", "year"])
    out["dblp_authors"].writerow(["key", "name"])
    people = {}   # pid -> (names, affiliation, wikidata, orcid)
    count = {}    # author name -> number of papers
    counts = {"records": 0, "papers": 0, "authorships": 0}
    with gzip.open(xml_path, "rb") as f:
        context = ET.iterparse(f, events=("start", "end"), parser=parser)
        _, root = next(context)
        for event, el in context:
            if event != "end" or el.tag not in PAPERS and el.tag != "www":
                continue
            counts["records"] += 1
            if el.tag == "www":
                key = el.get("key", "")
                if key.startswith("homepages/") and el.get("publtype") not in ("disambiguation", "group"):
                    urls = [u.text or "" for u in el.findall("url")]
                    people[key[len("homepages/"):]] = (
                        [a.text for a in el.findall("author") if a.text],
                        next((x.text for x in el.findall("note") if x.get("type") == "affiliation" and x.text), ""),
                        "|".join(sorted({u.rsplit("/", 1)[-1] for u in urls if "wikidata.org/entity/Q" in u})),
                        "|".join(sorted({u.rsplit("/", 1)[-1] for u in urls if "orcid.org/" in u})))
            else:
                authors = list(dict.fromkeys(a.text for a in el.findall("author") if a.text))
                for nm in authors:
                    count[nm] = count.get(nm, 0) + 1
                if 2 <= len(authors) <= MAX_AUTHORS:
                    t = el.find("title")
                    title = " ".join("".join(t.itertext()).split()).rstrip(".") if t is not None else ""
                    out["dblp_papers"].writerow([el.get("key"), title, el.findtext("year") or ""])
                    out["dblp_authors"].writerows([el.get("key"), nm] for nm in authors)
                    counts["papers"] += 1
                    counts["authorships"] += len(authors)
            root.clear()
    out["dblp_people"].writerow(["pid", "name", "affiliation", "papers", "wikidata", "orcid"])
    out["dblp_names"].writerow(["name", "pid"])
    counts["people"] = 0
    for pid, (aliases, aff, wd, orcid) in people.items():
        total = sum(count.get(nm, 0) for nm in aliases)
        if total:
            out["dblp_people"].writerow([pid, display_name(aliases[0]), aff, total, wd, orcid])
            out["dblp_names"].writerows([nm, pid] for nm in aliases)
            counts["people"] += 1
    for n, f in files.items():
        f.close()
        os.replace(os.path.join(raw_dir, n + ".csv.gz.part"), os.path.join(raw_dir, n + ".csv.gz"))
    return counts


def fetch(raw_dir: str, refresh: bool = False) -> None:
    t0 = time.time()
    path = download(raw_dir, refresh)
    outs = [os.path.join(raw_dir, n + ".csv.gz") for n in ("dblp_people", "dblp_names", "dblp_papers", "dblp_authors")]
    if not refresh and all(os.path.exists(p) and os.path.getmtime(p) >= os.path.getmtime(path) for p in outs):
        return
    log("  reading dblp.xml")
    counts = parse(path, raw_dir)
    log("  dblp: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()) + f" ({time.time() - t0:.0f}s)")
