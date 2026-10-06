"""TF-IDF text similarity between notice text and award descriptions, in plain Python."""

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping

_WORD = re.compile(r"[a-z0-9]+")

# Common English words plus federal-data boilerplate ("IGF::OT::IGF" marks inherently
# governmental functions in FPDS descriptions) and procurement filler.
_STOPWORD_TEXT = """
    a an and any are as at be by for from has have in into is it its of on or our such that
    the their this to was were will with within without shall should must may can all other
    igf ot ct cl cn fy po
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

Vector = dict[str, float]


def tokens(text: str | None) -> list[str]:
    """Lowercase words of 2+ characters that contain a letter and aren't stopwords."""
    if not text:
        return []
    return [
        word
        for word in _WORD.findall(text.lower())
        if len(word) >= 2 and not word.isdigit() and word not in STOPWORDS
    ]


class TextModel:
    """Document frequencies from a corpus; vectors are L2-normalized TF-IDF."""

    def __init__(self, document_frequency: Mapping[str, int], documents: int) -> None:
        self._df = dict(document_frequency)
        self._documents = documents

    @classmethod
    def from_texts(cls, texts: Iterable[str | None]) -> "TextModel":
        df: Counter[str] = Counter()
        documents = 0
        for text in texts:
            documents += 1
            df.update(set(tokens(text)))
        return cls(df, documents)

    def idf(self, word: str) -> float:
        return math.log((1 + self._documents) / (1 + self._df.get(word, 0))) + 1

    def vector(self, text: str | None) -> Vector:
        counts = Counter(tokens(text))
        weights = {word: (1 + math.log(n)) * self.idf(word) for word, n in counts.items()}
        norm = math.sqrt(sum(value * value for value in weights.values()))
        return {word: value / norm for word, value in weights.items()} if norm else {}


def cosine(a: Vector, b: Vector) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(value * b.get(word, 0.0) for word, value in a.items())


def shared_terms(a: Vector, b: Vector, limit: int = 3) -> tuple[str, ...]:
    """The words contributing most to the similarity, strongest first."""
    shared = sorted(
        (word for word in a if word in b), key=lambda word: (-(a[word] * b[word]), word)
    )
    return tuple(shared[:limit])
