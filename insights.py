"""Functions for the newest-first history returned by the existing data loader."""
from collections import defaultdict

FILTERS = {"全部": (None, None), "多人": ("modeType", "MULTI"),
           "单人": ("modeType", "SINGLE"), "极速": ("modeDifficulty", "FAST"),
           "终极": ("modeDifficulty", "ABYSS")}


def select_history(records, mode, result="全部"):
    if mode not in FILTERS or result not in ("全部", "完成", "中断"):
        raise ValueError("模式：全部/多人/单人/极速/终极；结果：全部/完成/中断")
    key, value = FILTERS[mode]
    return [(i, r) for i, r in enumerate(records, 1)
            if (not key or r.get(key) == value)
            and (result == "全部" or bool(r.get("completeState")) == (result == "完成"))]


def active_layers(record):
    """One entry per activated bond per game. None is not a measured zero."""
    layers = {}
    for b in record.get("bondInfo") or []:
        if not isinstance(b, dict) or not b.get("activate") or not b.get("bondId"):
            continue
        bid = b["bondId"]
        layers.setdefault(bid, None)
        try:
            layer = int(b["layer"])
        except (KeyError, ValueError, TypeError):
            continue
        if layer > 0:
            layers[bid] = max(layer, layers[bid] or 0)
    return layers


def rankings(records):
    samples = defaultdict(list)
    for r in records:
        for bid, layer in active_layers(r).items():
            samples[bid].append(layer)
    rows = []
    for bid, values in samples.items():
        numeric = [v for v in values if v is not None]
        rows.append((bid, len(values), max(numeric, default=None),
                     sum(numeric) / len(numeric) if numeric else None))
    return sorted(rows, key=lambda row: (-row[1], -(row[2] or 0), row[0]))
