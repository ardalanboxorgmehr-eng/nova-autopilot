#!/usr/bin/env python3
"""Merge this run's state files into the versions already on the branch.

Two runs of dm.yml can overlap (the concurrency group only queues, and a run on
another branch shares the group). When they do, `git pull --rebase` hits a
conflict in state/dm_sent.json, the whole commit step fails and the run's
records are thrown away. Everyone that run messaged is then messaged again on a
later run. Added 9 Nov 2026, when raising the per-run send cap made that cost
150 duplicate DMs instead of 40.

Usage: merge_state.py OURS_DIR   (the current checkout holds THEIRS)
Records are only ever added, never dropped, so an overlap costs nothing.
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def newer(a, b):
    """Of two dicts carrying an "at" timestamp, the later one."""
    if not isinstance(a, dict):
        return b
    if not isinstance(b, dict):
        return a
    return a if str(a.get("at", "")) >= str(b.get("at", "")) else b


def merge_sent(ours, theirs):
    """dm_sent.json: {media id: {comment id: record}} plus "_" bookkeeping keys.
    Union of the comment records, ours winning a straight clash. The single-value
    "_" keys take whichever run wrote last."""
    out = dict(theirs)
    for k, v in ours.items():
        if k not in out:
            out[k] = v
        elif isinstance(v, dict) and isinstance(out[k], dict):
            if k in ("_health", "_report", "_desk"):
                out[k] = newer(v, out[k])
            else:
                merged = dict(out[k])
                merged.update(v)          # a record is written once and not revised
                out[k] = merged
        else:
            out[k] = v
    return out


def merge_yt(ours, theirs):
    """yt_desk.json: "done" is a union, the rest is whichever run wrote last."""
    base = newer(ours, theirs)
    out = dict(base)
    done = dict(theirs.get("done") or {})
    done.update(ours.get("done") or {})
    out["done"] = done
    return out


def main():
    ours_dir = sys.argv[1]
    changed = []
    for name, how in (("dm_sent.json", merge_sent), ("yt_desk.json", merge_yt)):
        mine = read(os.path.join(ours_dir, name))
        if mine is None:
            continue
        here = os.path.join(ROOT, "state", name)
        theirs = read(here)
        write(here, mine if theirs is None else how(mine, theirs))
        changed.append(name)
    # desk_inbox.enc is sealed, so it cannot be merged: the newer snapshot wins.
    enc = os.path.join(ours_dir, "desk_inbox.enc")
    if os.path.exists(enc):
        with open(enc, "rb") as f:
            blob = f.read()
        with open(os.path.join(ROOT, "state", "desk_inbox.enc"), "wb") as f:
            f.write(blob)
        changed.append("desk_inbox.enc")
    print("merged: " + ", ".join(changed) if changed else "nothing to merge")


if __name__ == "__main__":
    main()
