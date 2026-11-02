#!/usr/bin/env python3
"""Six Degrees: a people database built from Wikidata with shortest-connection search.

  python3 sixdeg.py update                         # download from Wikidata + build (≈ 20-30 min)
  python3 sixdeg.py serve                          # web app at http://127.0.0.1:8765
  python3 sixdeg.py connect "Kevin Bacon" "Barack Obama"
  python3 sixdeg.py search bacon
  python3 sixdeg.py person "Ada Lovelace"
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SIXDEG_DATA", os.path.join(HERE, "data"))
sys.path.insert(0, HERE)

from sixdeg import CATEGORIES, DEFAULT_OFF  # noqa: E402


def _require_db():
    if not os.path.exists(os.path.join(DATA, "people.db")):
        sys.exit("No database yet: run `python3 sixdeg.py update` first.")


def cmd_fetch(args):
    from sixdeg.fetch import fetch_all
    fetch_all(os.path.join(DATA, "raw"), refresh=args.refresh, only=args.only)


def cmd_build(args):
    from sixdeg.build import build
    build(DATA, min_isolated_sitelinks=args.min_isolated_sitelinks, keep_stage=args.keep_stage,
          reuse_stage=getattr(args, "reuse_stage", False), new_people=not args.wikidata_people_only)


def cmd_update(args):
    cmd_fetch(args)
    cmd_build(args)


def cmd_serve(args):
    _require_db()
    from sixdeg.server import serve
    serve(DATA, args.host, args.port, open_browser=args.open)


def cmd_site(args):
    _require_db()
    from sixdeg import static_site
    out = os.path.abspath(args.out)
    if args.web_only:
        with open(os.path.join(out, "data", "meta.json")) as f:
            import json
            static_site.copy_web(out, json.load(f)["version"])
        print(f"web app files copied to {out}")
    else:
        static_site.export_site(DATA, out, web=not args.data_only)


def cmd_deploy(args):
    """Publish the exported site as the gh-pages branch of a GitHub repository. Each deploy is one
    parentless commit, force-pushed, so old data doesn't pile up in the repository; the local
    repository in site/.git is kept, so files the last deploy already sent aren't uploaded again."""
    import subprocess
    site = os.path.abspath(args.dir)
    if not os.path.isfile(os.path.join(site, "data", "meta.json")):
        sys.exit("no site yet: run `python3 sixdeg.py site` first")
    remote = args.repo if "://" in args.repo or args.repo.startswith("git@") else f"https://github.com/{args.repo}.git"
    git = lambda *a: subprocess.run(["git", *a], cwd=site, check=True)
    if not os.path.isdir(os.path.join(site, ".git")):
        git("init", "-q", "-b", "gh-pages")
    else:  # keep only what the last deploy pushed
        git("reflog", "expire", "--expire=now", "--all")
        git("gc", "-q", "--prune=now")
    git("checkout", "-q", "--orphan", "next-deploy")
    git("add", "-A")
    ident = []
    if args.author:
        name, _, email = args.author.partition("<")
        ident = ["-c", f"user.name={name.strip()}", "-c", f"user.email={email.rstrip('>').strip()}"]
    git(*ident, "commit", "-q", "-m", args.message)
    git("branch", "-M", "gh-pages")
    git("push", "-f", remote, "gh-pages")
    print(f"pushed {site} to the gh-pages branch of {remote}")


def cmd_serve_site(args):
    from sixdeg.static_server import serve_static
    serve_static(os.path.abspath(args.dir), args.host, args.port, open_browser=args.open)


def _cats(args):
    keys = [k for k, _ in CATEGORIES]
    chosen = set(keys) - DEFAULT_OFF
    if args.only:
        chosen = set(args.only.split(","))
    if args.include:
        chosen |= set(args.include.split(","))
    if args.exclude:
        chosen -= set(args.exclude.split(","))
    bad = chosen - set(keys)
    if bad:
        sys.exit(f"unknown link type(s): {', '.join(sorted(bad))}. Choose from: {', '.join(keys)}")
    return ",".join(sorted(chosen))


def _line(p):
    bits = [b for b in (p.get("years"), p.get("description")) if b]
    return p["name"] + (f"  ({' · '.join(bits)})" if bits else "") + f"  [{p['qid']}]"


def cmd_connect(args):
    _require_db()
    from sixdeg.server import App
    app = App(DATA)
    avoid = []
    for name in args.avoid or []:
        p = app.store.person_by_qid(name) or app.store.resolve(name)
        if not p:
            sys.exit(f"no person found for “{name}”")
        avoid.append(p["qid"])
    res = app.connect(args.a, args.b, _cats(args), ",".join(avoid), k=args.k, max_degrees=args.max)
    if "error" in res:
        sys.exit(res["error"])
    A, B = res["from"], res["to"]
    print(f"\n{A['name']} → {B['name']}")
    if res["degrees"] is None:
        print("  " + res["reason"])
        return
    print(f"{res['degrees']} degree{'s' if res['degrees'] != 1 else ''} of separation "
          f"(searched {res['visited']:,} people in {res['ms']} ms)\n")
    for n, path in enumerate(res["paths"]):
        if n == 1:
            print("Other routes of the same length:\n")
        if n == 0:
            for i, p in enumerate(path["people"]):
                print("  " + _line(p))
                if i < len(path["links"]):
                    link = path["links"][i]
                    also = link["also"]
                    extra = f"  (+{also['count']} more in common)" if also["count"] else ""
                    for j, chunk in enumerate(textwrap.wrap(link["sentence"] + extra, 86)):
                        print(("    └─ " if j == 0 else "       ") + chunk)
            print()
        else:
            print("  " + "  →  ".join(p["name"] for p in path["people"]))
    if len(res["paths"]) > 1:
        print()


def cmd_search(args):
    _require_db()
    from sixdeg.store import Store
    for p in Store(os.path.join(DATA, "people.db")).search(args.text, args.limit):
        print(_line(p))


def cmd_person(args):
    _require_db()
    from sixdeg.store import Store
    st = Store(os.path.join(DATA, "people.db"))
    p = st.person_by_qid(args.text) or st.resolve(args.text)
    if not p:
        sys.exit("not found")
    nb = st.neighborhood(p["id"], per_group=args.limit)
    print("\n" + _line(p))
    if p.get("wikipedia"):
        print(p["wikipedia"])
    labels = nb["labels"]
    for cat, items in nb["relations"].items():
        print(f"\n{labels[cat]}:")
        for it in items[: args.limit]:
            print("  • " + it["sentence"])
        if len(items) > args.limit:
            print(f"  … and {len(items) - args.limit} more")
    for cat, block in nb["contexts"].items():
        print(f"\n{labels[cat]} ({block['total']}):")
        for it in block["items"]:
            mates = ", ".join(m["name"] for m in it["with"])
            print(f"  • {it['title']} — {it['role'] or 'member'}" + (f"; with {mates}" if mates else ""))
    print()


def cmd_stats(args):
    _require_db()
    from sixdeg.store import Store
    s = Store(os.path.join(DATA, "people.db")).stats()
    for k, v in s.items():
        print(f"{k:12s} {v:,}" if isinstance(v, int) else f"{k:12s} {v}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def build_opts(p):
        p.add_argument("--min-isolated-sitelinks", type=int, default=1,
                       help="keep people with no links only if they have at least this many Wikipedia/"
                            "Wikimedia pages (default 1)")
        p.add_argument("--keep-stage", action="store_true",
                       help="keep the intermediate stage.db (lets the next build use --reuse-stage)")
        p.add_argument("--reuse-stage", action="store_true",
                       help="skip loading the raw CSVs and reuse a kept stage.db")
        p.add_argument("--wikidata-people-only", action="store_true",
                       help="use MusicBrainz and dblp only for links between people who are in Wikidata")

    def fetch_opts(p):
        p.add_argument("--refresh", action="store_true", help="re-download even if cached")
        p.add_argument("--only", nargs="*", help="only queries whose name starts with these prefixes")

    p = sub.add_parser("update", help="download from Wikidata and build the database")
    fetch_opts(p)
    build_opts(p)
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("fetch", help="download raw data only")
    fetch_opts(p)
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("build", help="build people.db and the graph from downloaded data")
    build_opts(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the app in your browser")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("site", help="export a static website (for GitHub Pages and the like)")
    p.add_argument("--out", default=os.path.join(HERE, "site"), help="output folder (default ./site)")
    p.add_argument("--data-only", action="store_true", help="write the data files only")
    p.add_argument("--web-only", action="store_true", help="only refresh the HTML/JS/CSS")
    p.set_defaults(func=cmd_site)
    p = sub.add_parser("deploy", help="publish the exported website to GitHub Pages (gh-pages branch)")
    p.add_argument("repo", help="OWNER/NAME of a GitHub repository, or a git remote URL")
    p.add_argument("--dir", default=os.path.join(HERE, "site"))
    p.add_argument("--author", help='commit author, "Name <email>" (default: your git config)')
    p.add_argument("-m", "--message", default="Six Degrees static site", help="commit message")
    p.set_defaults(func=cmd_deploy)
    p = sub.add_parser("serve-site", help="preview the exported static website locally")
    p.add_argument("dir", nargs="?", default=os.path.join(HERE, "site"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--open", action="store_true", help="open it in your browser")
    p.set_defaults(func=cmd_serve_site)

    keys = ", ".join(k for k, _ in CATEGORIES)
    p = sub.add_parser("connect", help="find the shortest connection between two people")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--only", help=f"comma list of link types to use ({keys})")
    p.add_argument("--include", help="add link types that are off by default (awards, influence)")
    p.add_argument("--exclude", help="drop link types")
    p.add_argument("--avoid", nargs="*", help="people the route must not pass through")
    p.add_argument("-k", type=int, default=4, help="how many alternative routes to list")
    p.add_argument("--max", type=int, help="give up beyond this many degrees (default: no limit)")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("search", help="look people up by name")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("person", help="show someone's direct connections")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_person)

    p = sub.add_parser("stats", help="database size")
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
