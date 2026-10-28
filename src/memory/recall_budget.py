"""Recall budget, Aidon recall_budget concept, rebuilt.

Recall is not free: a recall budget caps how many points, how many tokens, and how many
collections a single recall can spend. check_recall(points, budget) returns whether the
recall fits, and how much it spent, so the loop can trim a recall before it spends the
whole step budget, instead of recalling everything and hoping the model copes.
"""

from __future__ import annotations


class RecallBudget:
    def __init__(self, max_points: int = 20, max_tokens: int = 4000, max_collections: int = 3):
        self.max_points = max_points
        self.max_tokens = max_tokens
        self.max_collections = max_collections

    def check(self, points: list, collections: list) -> dict:
        tokens = sum(len(str(p.get("text", ""))) // 4 if isinstance(p, dict) else len(str(p)) // 4 for p in points)
        over = []
        if len(points) > self.max_points:
            over.append("points")
        if tokens > self.max_tokens:
            over.append("tokens")
        if len(collections) > self.max_collections:
            over.append("collections")
        return {"ok": not over, "over": over, "points": len(points), "tokens": tokens, "collections": len(collections)}

    def trim(self, points: list) -> list:
        return points[: self.max_points]
