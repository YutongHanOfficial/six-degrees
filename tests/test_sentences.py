"""Wording of link explanations. Run: python3 tests/test_sentences.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sixdeg.store import _the, context_sentence, relation_sentence, season_ranges  # noqa: E402

OBAMA = {"id": 1, "name": "Barack Obama", "gender": "m"}
MALIA = {"id": 2, "name": "Malia Obama", "gender": "f"}
BUSH = {"id": 3, "name": "George W. Bush", "gender": "m"}


def ctx(kind, category, name, year=None, period=None):
    return {"kind": kind, "category": category, "name": name, "year": year, "period": period}


CASES = [
    (relation_sentence({"type": "parent", "a_id": 1, "detail": None, "year": None}, OBAMA, MALIA),
     "Barack Obama is the father of Malia Obama"),
    (relation_sentence({"type": "parent", "a_id": 1, "detail": None, "year": None}, MALIA, OBAMA),
     "Malia Obama is the daughter of Barack Obama"),
    (relation_sentence({"type": "succession", "a_id": 3, "detail": "President of the United States", "year": 2009},
                       OBAMA, BUSH),
     "Barack Obama succeeded George W. Bush as President of the United States in 2009"),
    (relation_sentence({"type": "relative", "a_id": 1, "detail": "cousin", "year": None}, OBAMA, MALIA),
     "Malia Obama is Barack Obama's cousin"),
    (context_sentence(ctx("work", "film", "Apollo 13", 1995), OBAMA, BUSH, {"cast member"}, {"cast member"}),
     "Barack Obama and George W. Bush both appeared in Apollo 13 (1995)"),
    (context_sentence(ctx("work", "film", "Apollo 13", 1995), OBAMA, BUSH, {"cast member"}, {"director"}),
     "George W. Bush directed Barack Obama in Apollo 13 (1995)"),
    (context_sentence(ctx("team", "sports", "Villarreal CF", period=2021), OBAMA, BUSH, {"player"}, {"head coach"},
                      "Villarreal CF (2021–24)"),
     "George W. Bush coached Barack Obama at Villarreal CF (2021–24)"),
    (context_sentence(ctx("election", "politics", "2008 United States presidential election"), OBAMA, BUSH,
                      {"candidate"}, {"candidate"}),
     "Barack Obama and George W. Bush were both candidates in the 2008 United States presidential election"),
    (context_sentence(ctx("award", "awards", "Nobel Prize in Physics", period=1903), OBAMA, BUSH,
                      {"recipient"}, {"recipient"}),
     "Barack Obama and George W. Bush both received the Nobel Prize in Physics in 1903"),
    (context_sentence(ctx("group", "music", "The Beatles"), OBAMA, BUSH, {"member"}, {"member"}),
     "Barack Obama and George W. Bush were bandmates in The Beatles"),
    # kinship recorded from the wrong side: a "grandchild" born 168 years earlier
    (relation_sentence({"type": "relative", "a_id": 4, "detail": "great-great-great-great-grandchild", "year": None},
                       {"id": 4, "name": "Michael C. Burgess", "birth_year": 1956},
                       {"id": 5, "name": "Lord Byron", "birth_year": 1788}),
     "Michael C. Burgess is Lord Byron's great-great-great-great-grandchild"),
    (relation_sentence({"type": "relative", "a_id": 5, "detail": "great-great-great-great-grandchild", "year": None},
                       {"id": 5, "name": "Lord Byron", "birth_year": 1788},
                       {"id": 4, "name": "Michael C. Burgess", "birth_year": 1956}),
     "Michael C. Burgess is Lord Byron's great-great-great-great-grandchild"),
    (season_ranges([2021, 2022, 2023, 2026]), "2021–24, 2026–27"),
    (season_ranges([1999, 2000]), "1999–2001"),
    (_the("Live Aid"), "Live Aid"),
    (_the("Treaty of Versailles"), "the Treaty of Versailles"),
]


def main():
    bad = [(got, want) for got, want in CASES if got != want]
    for got, want in bad:
        print(f"got:  {got}\nwant: {want}\n")
    if bad:
        sys.exit(f"{len(bad)} of {len(CASES)} failed")
    print(f"ok: {len(CASES)} sentences")


if __name__ == "__main__":
    main()
