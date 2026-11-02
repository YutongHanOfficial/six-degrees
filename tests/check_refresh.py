"""Refuses to publish a data refresh that lost a lot of data (a sign that a download went wrong),
or that is too big for GitHub Pages.

  python3 tests/check_refresh.py live/meta.json site/data/meta.json [--allow-drop 0.05]

Compares the stats of the published site with the new export, and exits 1 if people, relations,
contexts or memberships dropped by more than the allowed fraction. Wikidata only grows from month
to month, so a real drop that large means a query came back short.
"""
import argparse
import json
import os
import sys

KEYS = ("people", "relations", "contexts", "memberships")
MAX_SITE = 1_000_000_000   # GitHub Pages publishes sites of up to 1 GB


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("live", help="meta.json of the published site")
    ap.add_argument("new", help="meta.json of the new export")
    ap.add_argument("--allow-drop", type=float, default=0.05, help="largest allowed drop (default 0.05 = 5%%)")
    args = ap.parse_args()
    site = os.path.dirname(os.path.dirname(os.path.abspath(args.new)))
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, files in os.walk(site) if ".git" not in d
               for f in files)
    print(f"site size {size / 1e6:,.0f} MB")
    if size > MAX_SITE:
        print(f"not publishing: GitHub Pages takes sites of up to {MAX_SITE / 1e9:.0f} GB")
        return 1
    try:
        with open(args.live) as f:
            live = json.load(f)["stats"]
    except (OSError, ValueError, KeyError):
        print("nothing published yet to compare with")
        return 0
    with open(args.new) as f:
        new = json.load(f)["stats"]
    print(f"published {live['built_at']}, new {new['built_at']}")
    shrunk = []
    for k in KEYS:
        change = new[k] / live[k] - 1
        print(f"  {k:12s} {live[k]:>12,} -> {new[k]:>12,}  ({change:+.1%})")
        if change < -args.allow_drop:
            shrunk.append(k)
    if shrunk:
        print(f"not publishing: {', '.join(shrunk)} dropped by more than {args.allow_drop:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
