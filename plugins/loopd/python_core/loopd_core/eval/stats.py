"""Statistical helpers: mean, std. Pure stdlib."""
from __future__ import annotations
import math
from typing import Sequence

def mean(values: Sequence[float]) -> float | None:
    lst = [v for v in values if v is not None]
    if not lst:
        return None
    return sum(lst) / len(lst)

def std(values: Sequence[float]) -> float | None:
    lst = [v for v in values if v is not None]
    n = len(lst)
    if n < 2:
        return None
    m = sum(lst) / n
    return math.sqrt(sum((v - m) ** 2 for v in lst) / (n - 1))
