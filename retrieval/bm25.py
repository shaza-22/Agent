"""BM25 lexical search, pure Python, no dependencies.

What: a standard Okapi BM25 over the same retrieval records, with a tokenizer
      that handles Arabic as well as English.
Inputs: retrieval records; a query string.
Outputs: (record index, score) pairs.
Why: two reasons, both load-bearing here.
     1. Fees, rates and product names are matched *lexically*. A dense model
        will happily rank a page about "card benefits" above the one holding
        "EGP 350", because they are semantically close. BM25 will not.
     2. It needs no model download, so retrieval works offline and degrades
        gracefully when the embedding model is unavailable.

Arabic normalisation matters: أ إ آ all normalise to ا, ة to ه, ى to ي, and
diacritics are stripped. Without it "رسوم البطاقات" and "رسوم البطاقة" are
different tokens and the query silently misses.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# Arabic letters/digits, but NOT Arabic punctuation (؟ ، ؛ ٪ ۔), which the
# broad \u0600-\u06FF range would otherwise glue onto the end of a token.
_ARABIC_PUNCT = re.compile(r"[\u060C\u061B\u061F\u066A-\u066D\u06D4\u00AB\u00BB]")
_TOKEN_RE = re.compile(r"[\w\u0621-\u064A\u0660-\u0669\u0671-\u06D3]+", re.UNICODE)
_DIACRITICS = re.compile(r"[ً-ْـ]")        # tashkeel + tatweel
_ALEF = re.compile(r"[آأإٱ]")          # آ أ إ ٱ
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Words too common to discriminate. Kept small and explicit.
STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "at", "is",
    "are", "what", "which", "do", "does", "i", "my", "me", "you", "your",
    "من", "في", "على", "الى", "إلى", "عن", "ما", "هي", "هو", "و", "او", "أو",
}


def normalise_arabic(text: str) -> str:
    text = _DIACRITICS.sub("", text)
    text = _ALEF.sub("ا", text)
    text = text.replace("ة", "ه")   # ة -> ه
    text = text.replace("ى", "ي")   # ى -> ي
    return text.translate(_ARABIC_DIGITS)


def tokenize(text: str) -> list[str]:
    text = _ARABIC_PUNCT.sub(" ", normalise_arabic(text.lower()))
    return [t for t in _TOKEN_RE.findall(text)
            if t not in STOPWORDS and len(t) > 1]


class BM25:
    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5,
                 b: float = 0.75):
        self.k1, self.b = k1, b
        self.n_docs = len(corpus_tokens)
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avg_len = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0
        self.freqs: list[Counter] = [Counter(d) for d in corpus_tokens]

        df = Counter()
        for tokens in corpus_tokens:
            df.update(set(tokens))
        # Standard BM25+ idf floor, so a term in almost every document cannot
        # contribute a negative score and drag a good match down.
        self.idf = {
            term: math.log(1 + (self.n_docs - n + 0.5) / (n + 0.5))
            for term, n in df.items()
        }

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        q_tokens = tokenize(query)
        if not q_tokens or not self.n_docs:
            return []
        scores = [0.0] * self.n_docs
        for term in q_tokens:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, freq in enumerate(self.freqs):
                f = freq.get(term)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b *
                                       self.doc_len[i] / (self.avg_len or 1))
                scores[i] += idf * (f * (self.k1 + 1)) / denom
        ranked = sorted(((i, s) for i, s in enumerate(scores) if s > 0),
                        key=lambda x: -x[1])
        return ranked[:top_k]
