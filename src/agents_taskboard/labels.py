"""Label normalisation: free-form tags, trimmed and lowercased, no registry."""

from __future__ import annotations

import re

MAX_LABELS = 20
MAX_LABEL_LENGTH = 40
_LABEL = re.compile(r"[a-z0-9_.:-]{1,40}")


def normalize_labels(raw: list[str]) -> list[str]:
    """Trim, lowercase and dedupe (keeping order); raise ValueError on a bad label or > 20."""
    out: list[str] = []
    for item in raw:
        label = item.strip().lower()
        if not _LABEL.fullmatch(label):
            raise ValueError(
                f"invalid label {item!r}: 1-{MAX_LABEL_LENGTH} characters of [a-z0-9-_.:]"
            )
        if label not in out:
            out.append(label)
    if len(out) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} labels")
    return out
