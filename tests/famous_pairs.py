"""Runs a batch of well-known pairs against the real database and prints the routes and timings.

Run: python3 tests/famous_pairs.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sixdeg.server import App  # noqa: E402

PAIRS = [
    ("Kevin Bacon", "Barack Obama"),
    ("Taylor Swift", "Isaac Newton"),
    ("Albert Einstein", "Marie Curie"),
    ("Elizabeth II", "Elvis Presley"),
    ("Lionel Messi", "Cristiano Ronaldo"),
    ("Ada Lovelace", "Alan Turing"),
    ("William Shakespeare", "Steve Jobs"),
    ("Neil Armstrong", "Michael Jordan"),
    ("Napoleon", "Beyoncé"),
    ("Genghis Khan", "Bill Gates"),
    ("Frida Kahlo", "Leon Trotsky"),
    ("Serena Williams", "Winston Churchill"),
]


def main():
    data = os.environ.get("SIXDEG_DATA", os.path.join(os.path.dirname(__file__), "..", "data"))
    t0 = time.time()
    app = App(data)
    print(f"loaded in {time.time() - t0:.1f}s\n")
    worst = 0
    for a, b in PAIRS:
        res = app.connect(a, b, k=3)
        if "error" in res:
            print(f"{a} → {b}: {res['error']}\n")
            continue
        worst = max(worst, res["ms"])
        print(f"{res['from']['name']} → {res['to']['name']}: {res['degrees']} degrees, "
              f"{res['ms']} ms, {res['visited']:,} visited")
        if res["paths"]:
            for link in res["paths"][0]["links"]:
                print("   - " + link["sentence"])
        else:
            print("   " + res.get("reason", ""))
        print()
    print(f"slowest search: {worst} ms")


if __name__ == "__main__":
    main()
