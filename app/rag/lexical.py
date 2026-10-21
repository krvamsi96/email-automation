"""
Keyword (BM25) search over a workspace's knowledge chunks.

Dense embeddings capture meaning but are weak at exact tokens such as reference
numbers, codes, user names and e-mail addresses. This small in-memory BM25 index
complements the vector search; the retriever rebuilds it from the vector store
whenever the store changes.
"""

import heapq
import math
import re
from collections import Counter, defaultdict
from typing import Dict, List, Sequence, Tuple

# Letter runs (any language) and digit runs: "PA-23" -> pa, 23; "Abc#2021" -> abc, 2021
TOKEN_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)

STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have how i if in into is it its
may me my no not of on or our please so that the their them then there these they this those to us was
we were what when where which who whom why will with would you your kindly sir madam dear regards
""".split())


def tokenize(text: str) -> List[str]:
    tokens = []
    for tok in TOKEN_RE.findall((text or "").casefold()):
        if tok in STOPWORDS or (len(tok) == 1 and not tok.isdigit()):
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") and not tok.isdigit():
            tok = tok[:-1]  # light plural folding: passwords -> password
        tokens.append(tok)
    return tokens


class BM25Index:
    """Okapi BM25 over a fixed set of texts."""

    def __init__(self, ids: Sequence[str], texts: Sequence[str], k1: float = 1.5, b: float = 0.75):
        self.ids = list(ids)
        self.k1, self.b = k1, b
        self.lengths: List[int] = []
        self.postings: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        for i, text in enumerate(texts):
            counts = Counter(tokenize(text))
            self.lengths.append(sum(counts.values()))
            for term, freq in counts.items():
                self.postings[term].append((i, freq))
        n = len(self.ids)
        self.avg_length = (sum(self.lengths) / n) if n else 1.0
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}
        # IDF a query term would have if it appeared nowhere (upper bound for coverage)
        self.max_idf = math.log(1 + (n + 0.5) / 0.5) if n else 1.0

    def __len__(self) -> int:
        return len(self.ids)

    def search(self, query: str, k: int) -> List[Tuple[str, float, float, int]]:
        """
        Returns up to k (id, bm25_score, coverage, matched_terms) tuples, best first.
        coverage is the share of the query's informative weight (IDF) that the chunk contains;
        matched_terms is the number of distinct query terms found in the chunk.
        """
        terms = set(tokenize(query))
        if not terms or not self.ids:
            return []
        total_weight = sum(self.idf.get(t, self.max_idf) for t in terms)
        scores: Dict[int, float] = defaultdict(float)
        matched: Dict[int, float] = defaultdict(float)
        count: Dict[int, int] = defaultdict(int)
        for term in terms:
            idf = self.idf.get(term)
            if not idf:
                continue
            for i, freq in self.postings[term]:
                norm = freq + self.k1 * (1 - self.b + self.b * self.lengths[i] / self.avg_length)
                scores[i] += idf * freq * (self.k1 + 1) / norm
                matched[i] += idf
                count[i] += 1
        best = heapq.nlargest(k, scores.items(), key=lambda kv: kv[1])
        return [(self.ids[i], score, matched[i] / total_weight, count[i]) for i, score in best]
