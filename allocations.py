"""Shared allocation helpers (no DB / no UI dependencies)."""

from __future__ import annotations

import math


def allocate_integer_proportional(total: int, weights: list[float]) -> list[int]:
    """
    Split `total` into integers proportional to `weights` (largest remainder method).
    Sum of result always equals `total` when weights are non-empty and sum(weights) > 0.
    If `total` <= 0 or weights empty, returns all zeros. If sum(weights) == 0, splits
    `total` evenly across indices (remainder to low indices).
    """
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    s = float(sum(weights))
    if s <= 0:
        base, rem = divmod(total, n)
        return [base + (1 if i < rem else 0) for i in range(n)]
    raw = [total * float(w) / s for w in weights]
    floors = [int(math.floor(x)) for x in raw]
    assigned = sum(floors)
    rem = total - assigned
    order = sorted(range(n), key=lambda i: (raw[i] - floors[i], i), reverse=True)
    out = list(floors)
    for j in range(rem):
        out[order[j]] += 1
    return out

