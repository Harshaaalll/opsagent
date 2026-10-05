"""Company knowledge: policy clauses + lessons learned from past runs.

Ported from sanwaad's clause store. Two ideas carry over:

* Chunk on CLAUSE markers (`## [AP-02] Heading`), not fixed token windows. A
  clause is the unit a policy owner signs off on, so it is the unit to cite.
* Document expansion: a clause may carry `> also: ...` listing the words
  people actually use ("vendor bill", "payable"). Aliases are indexed, not
  shown, bridging vocabulary no retriever bridges on its own.

Retrieval is BM25 (no model download, deterministic, explainable). Dense
embeddings are the obvious next step; see README "What I'd build next".

Memory has a second source: `lessons.jsonl`, written by the runtime after each
run (what failed, how it was recovered). It is retrieved with the same BM25 so
the agent improves from outcomes without any fine-tuning.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_CLAUSE = re.compile(r"^##\s*\[([A-Z]{2,5}-\d{2})\]\s*(.+)$", re.M)
_ALIAS = re.compile(r"^>\s*also:\s*(.+)$", re.M)
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an the of to and or in on for is are be it this that with as at by from".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass
class Clause:
    id: str
    heading: str
    text: str
    source: str
    aliases: str = ""

    def index_text(self) -> str:
        return f"{self.heading} {self.text} {self.aliases}"


def parse_clauses(path: Path) -> list[Clause]:
    raw = path.read_text(encoding="utf-8")
    ms = list(_CLAUSE.finditer(raw))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(raw)
        body = raw[m.end():end].strip()
        aliases = ""
        am = _ALIAS.search(body)
        if am:
            aliases = am.group(1).strip()
            body = _ALIAS.sub("", body).strip()
        out.append(Clause(m.group(1), m.group(2).strip(), body, path.stem, aliases))
    return out


class Knowledge:
    def __init__(self, clauses: list[Clause], k1: float = 1.5, b: float = 0.75):
        self.clauses = clauses
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(c.index_text())) for c in clauses]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(clauses)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def load(cls, policy_dir: Path, lessons_path: Optional[Path] = None) -> "Knowledge":
        clauses: list[Clause] = []
        for p in sorted(policy_dir.glob("*.md")):
            clauses.extend(parse_clauses(p))
        if lessons_path and lessons_path.exists():
            for i, line in enumerate(lessons_path.read_text(encoding="utf-8").splitlines()):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                clauses.append(Clause(f"LRN-{i + 1:02d}", d.get("title", "lesson"), d.get("lesson", ""),
                                      "lessons", d.get("tags", "")))
        return cls(clauses)

    def search(self, query: str, k: int = 4) -> list[tuple[Clause, float]]:
        q = tokenize(query)
        scored = []
        for i, c in enumerate(self.clauses):
            tf, ln = self._tf[i], self._len[i]
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                s += self._idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self._avg))
            if s > 0:
                scored.append((c, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def get(self, clause_id: str) -> Optional[Clause]:
        return next((c for c in self.clauses if c.id == clause_id), None)


def append_lesson(path: Path, title: str, lesson: str, tags: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"title": title, "lesson": lesson, "tags": tags}, ensure_ascii=False) + "\n")
