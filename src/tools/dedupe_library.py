#!/usr/bin/env python3
"""Remove duplicate stickers/GIFs from the saved library (archive/web/user/stickers|gifs) by content sha256.
Per group of identical files one is kept (favorite > most recently used > oldest) and gets fav = any, last = max, added = min.
Dry run by default; --apply to change anything (meta is backed up to <meta>.bak first). Usage: dedupe_library.py [--apply] [stickers|gifs]"""
import hashlib, json, os, shutil, sys, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import waconf

LIB = {"stickers": ("webp", "stickers.json"), "gifs": ("mp4", "gifs.json")}


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
    return h.hexdigest()


def plan(meta, files):
    """meta {id: {...}}, files {id: sha256} -> ({keeper: merged meta}, [ids to remove]). Ids without a file are dropped from meta too."""
    groups = {}
    for i in meta:
        if i in files: groups.setdefault(files[i], []).append(i)
    keep, drop = {}, [i for i in meta if i not in files]
    for ids in groups.values():
        ids.sort(key=lambda i: (not meta[i].get("fav"), -meta[i].get("last", 0), meta[i].get("added", 0), i))
        m = dict(meta[ids[0]])
        m["fav"] = int(any(meta[i].get("fav") for i in ids))
        m["last"] = max(meta[i].get("last", 0) for i in ids)
        m["added"] = min(meta[i].get("added", 0) or int(time.time()) for i in ids)
        keep[ids[0]] = m
        drop += ids[1:]
    return keep, drop


def run(kind, apply):
    ext, mf = LIB[kind]
    d = os.path.join(waconf.USER, kind)
    mp = os.path.join(d, mf)
    try:
        with open(mp, encoding="utf-8") as f: meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    files = {i: sha(os.path.join(d, f"{i}.{ext}")) for i in meta if os.path.isfile(os.path.join(d, f"{i}.{ext}"))}
    keep, drop = plan(meta, files)
    print(f"{kind}: {len(meta)} saved, {len(keep)} unique, {len(drop)} to remove")
    for i in drop: print("  remove", i)
    if apply and drop:
        shutil.copy2(mp, mp + ".bak")
        for i in drop:
            try: os.remove(os.path.join(d, f"{i}.{ext}"))
            except OSError: pass
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(keep, f, ensure_ascii=False)
        os.replace(tmp, mp)
    return len(drop)


def check():
    meta = {"a": {"fav": 0, "last": 5, "added": 10}, "b": {"fav": 1, "last": 0, "added": 20}, "c": {"fav": 0, "last": 9, "added": 5}, "gone": {}}
    keep, drop = plan(meta, {"a": "X", "b": "X", "c": "Y"})
    assert keep["b"] == {"fav": 1, "last": 5, "added": 10} and sorted(drop) == ["a", "gone"] and set(keep) == {"b", "c"}
    print("dedupe_library: ok")


if __name__ == "__main__":
    if "--check" in sys.argv: sys.exit(check())
    kinds = [a for a in sys.argv[1:] if a in LIB] or list(LIB)
    n = sum(run(k, "--apply" in sys.argv) for k in kinds)
    if n and "--apply" not in sys.argv: print("dry run: nothing changed (use --apply)")
