"""Score detector output against your hand labels.

Inputs
  dataset/ground_truth.csv   recording,start,end,event,interesting,notes   (times in s or mm:ss)
  dataset/recordings.csv     recording,game,genre                         (optional, for per-game stats)
  analysis/<recording>/candidates.json + analysis.json                  (from detect.py)

Matching rule (fixed, documented):
  a candidate HITS a label if the candidate's peak lies inside the label interval,
  OR the two intervals overlap by >= min(2 s, 50% of the label length).

  Recall@K     = distinct interesting labels hit by the top-K candidates / all interesting labels
                 (a label counts once no matter how many candidates hit it)
  Precision@K  = top-K candidates that are the FIRST hit on an interesting label / top-K shown
                 (extra hits on an already-found label are 'duplicates' and count against precision;
                  candidates on unlabelled footage count against precision too - label generously)
  FP/hour      = top-K candidates that are not a first hit, per hour of footage

Usage:  python scripts/evaluate.py [--k 10]
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict

from gc_common import ANALYSIS_DIR, DATASET_DIR, load_ground_truth


def hits(cand: dict, label: dict) -> bool:
    if label["start"] <= cand["peak_t"] <= label["end"]:
        return True
    overlap = min(cand["end"], label["end"]) - max(cand["start"], label["start"])
    need = min(2.0, 0.5 * (label["end"] - label["start"]))
    return overlap >= need and overlap > 0


def score_recording(cands: list[dict], labels: list[dict], k: int, hours: float) -> dict:
    top = sorted(cands, key=lambda c: c.get("rank", 0))[:k]
    pos = [i for i, l in enumerate(labels) if l["interesting"]]
    neg = [i for i, l in enumerate(labels) if not l["interesting"]]
    found: set[int] = set()
    outcome = []
    for c in top:
        pos_hit = [i for i in pos if hits(c, labels[i])]
        new = [i for i in pos_hit if i not in found]
        if new:
            found.add(new[0])
            outcome.append("hit")
        elif pos_hit:
            outcome.append("duplicate")
        elif any(hits(c, labels[i]) for i in neg):
            outcome.append("known_negative")
        else:
            outcome.append("unlabelled")
    tp = outcome.count("hit")
    by_event = defaultdict(lambda: [0, 0])
    for i in pos:
        by_event[labels[i]["event"] or "?"][1] += 1
        if i in found:
            by_event[labels[i]["event"] or "?"][0] += 1
    return {
        "candidates_total": len(cands),
        "shown": len(top),
        "labels_interesting": len(pos),
        "labels_negative": len(neg),
        "recall_at_k": round(len(found) / len(pos), 3) if pos else None,
        "precision_at_k": round(tp / len(top), 3) if top else None,
        "hits": tp,
        "duplicates": outcome.count("duplicate"),
        "known_negative": outcome.count("known_negative"),
        "unlabelled": outcome.count("unlabelled"),
        "fp_per_hour": round((len(top) - tp) / hours, 1) if hours else None,
        "missed": [f"{labels[i]['event']} {labels[i]['start']:.0f}-{labels[i]['end']:.0f}s"
                   for i in pos if i not in found],
        "recall_by_event": {e: f"{a}/{b}" for e, (a, b) in by_event.items()},
        "outcome_by_rank": outcome,
    }


def load_meta() -> dict:
    path = DATASET_DIR / "recordings.csv"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["recording"].strip(): r for r in csv.DictReader(f) if r.get("recording")}


def aggregate(results: dict, meta: dict, key: str) -> dict:
    groups = defaultdict(lambda: {"found": 0, "pos": 0, "tp": 0, "shown": 0, "hours": 0.0})
    for rec, r in results.items():
        g = groups[(meta.get(rec) or {}).get(key, "?") or "?"]
        g["found"] += round((r["recall_at_k"] or 0) * r["labels_interesting"])
        g["pos"] += r["labels_interesting"]
        g["tp"] += r["hits"]
        g["shown"] += r["shown"]
        g["hours"] += r["_hours"]
    return {name: {
        "recall_at_k": round(g["found"] / g["pos"], 3) if g["pos"] else None,
        "precision_at_k": round(g["tp"] / g["shown"], 3) if g["shown"] else None,
        "fp_per_hour": round((g["shown"] - g["tp"]) / g["hours"], 1) if g["hours"] else None,
    } for name, g in groups.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    labels_all = load_ground_truth()
    if not labels_all:
        raise SystemExit(f"No labels yet: create {DATASET_DIR / 'ground_truth.csv'} (see the template file)")
    meta = load_meta()
    results = {}
    for rec in sorted({l["recording"] for l in labels_all}):
        d = ANALYSIS_DIR / rec
        if not (d / "candidates.json").exists():
            print(f"  - {rec}: no detector output yet (run detect.py first)")
            continue
        cands = json.loads((d / "candidates.json").read_text(encoding="utf-8"))
        info = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
        hours = info["media"]["duration_s"] / 3600
        r = score_recording(cands, [l for l in labels_all if l["recording"] == rec], args.k, hours)
        r["_hours"] = hours
        r["x_realtime"] = info.get("x_realtime")
        r["profile"] = info.get("profile")
        results[rec] = r
        print(f"{rec:<28} recall@{args.k}={r['recall_at_k']}  precision@{args.k}={r['precision_at_k']}  "
              f"fp/h={r['fp_per_hour']}  dup={r['duplicates']}  speed={r['x_realtime']}x  "
              f"missed={len(r['missed'])}")
    report = {"k": args.k, "recordings": results,
              "by_game": aggregate(results, meta, "game"), "by_genre": aggregate(results, meta, "genre")}
    for r in results.values():
        r.pop("_hours", None)
    out = ANALYSIS_DIR / "evaluation.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nby genre: {report['by_genre']}\nSaved {out}")


if __name__ == "__main__":
    main()
