"""Experiment cards: what a past flight changed, what moved, why, and the lesson.

The agent drafts a card at the end of each run (what changed, what moved, its candidate
explanations). A person confirms it by adding the cause and the lesson. Only confirmed cards
are cited in a readout as learnings; drafts are listed as "related drafts" with no claim, and
a flight's own cards never count as similar to itself.

Similarity: the same layer first, then shared tags and shared metric families that moved.
"""

import json
import os
import time


def draft(run_id, cfg, experiment, pairs, families):
    moved, fam_moved, candidates = [], set(), set()
    reverse = {m.lower(): f for f, m in families.items()}
    for p in pairs:
        for q in p["explained"]["questions"]:
            if q["change"].startswith("didn't move"):
                continue  # a metric that didn't move says nothing about what the flight is like
            moved.append({"pair": p["test"], "metric": q["metric"], "change": q["change"]})
            if q["metric"].lower() in reverse:
                fam_moved.add(reverse[q["metric"].lower()])
        candidates.update(i["id"] for i in p["explained"]["patterns"])
    changed = sorted({k for p in pairs for k in (p["m0_details"].get("diffs") or [])})
    return {"experiment_name": experiment, "name": (cfg or {}).get("name") or experiment, "layer": (cfg or {}).get("layer"),
            "tags": (cfg or {}).get("tags") or [], "status": "drafted", "what_changed": changed, "what_moved": moved[:12],
            "families_moved": sorted(fam_moved), "candidates": sorted(candidates), "cause": None, "lesson": None,
            "confirmed_by": None, "confirmed_on": None, "source_run": run_id,
            "drafted_on": time.strftime("%Y-%m-%d", time.gmtime())}


def load(folder):
    cards = []
    if folder and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json"):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    cards.append(json.load(handle))
    return cards


def save(folder, card):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{card['experiment_name']}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            old = json.load(handle)
        if old.get("status") == "confirmed":
            return path  # a person's confirmed card is never overwritten by a draft
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(card, handle, indent=1)
    return path


def confirm(folder, experiment, cause, lesson, who):
    path = os.path.join(folder, f"{experiment}.json")
    with open(path, encoding="utf-8") as handle:
        card = json.load(handle)
    card.update(status="confirmed", cause=cause, lesson=lesson, confirmed_by=who,
                confirmed_on=time.strftime("%Y-%m-%d", time.gmtime()))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(card, handle, indent=1)
    return card


def similar(cards, experiment, layer, tags, families, limit=2):
    """(confirmed cards to cite, related drafts) for this flight, best first."""
    scored = []
    for c in cards:
        if c["experiment_name"] == experiment:
            continue
        shared = len(set(tags or []) & set(c.get("tags") or [])) + len(set(families or []) & set(c.get("families_moved") or []))
        if not shared:
            continue  # the same layer alone doesn't make two experiments similar
        score = (3 if layer and c.get("layer") == layer else 0) + shared
        if score >= 2:
            scored.append((score, c))
    scored.sort(key=lambda sc: -sc[0])
    confirmed = [c for _, c in scored if c.get("status") == "confirmed"][:limit]
    drafts = [c for _, c in scored if c.get("status") != "confirmed"][:limit]
    return confirmed, drafts
