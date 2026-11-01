"""Six Degrees: a people database built from Wikidata, plus shortest-connection search."""

CATEGORIES = [
    # code order matters: it is stored in the graph files
    ("family", "Family"),
    ("film", "Film & TV"),
    ("music", "Music"),
    ("sports", "Sports"),
    ("politics", "Politics & office"),
    ("science", "Science & teaching"),
    ("business", "Business"),
    ("arts", "Books & art"),
    ("other", "Other"),
    ("awards", "Awards"),
    ("influence", "Influence"),
]
CATEGORY_CODE = {key: i for i, (key, _) in enumerate(CATEGORIES)}
# Weak, "never actually met" style links are opt-in.
DEFAULT_OFF = {"awards", "influence"}
