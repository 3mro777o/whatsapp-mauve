#!/usr/bin/env python3
"""Regenerate tools/web-static/emoji.json from unicode.org's emoji-test.txt (fully-qualified, default skin tone only).
Usage: build_emoji.py [--max-version 16.0]   (newer emoji may show as tofu on old fonts; default keeps everything)
Output: [{"g": "Smileys & Emotion", "e": ["😀", ...]}, ...]  (group "Component" = bare skin-tone/hair swatches is left out)"""
import json, os, re, sys, tempfile, urllib.request

URL = "https://unicode.org/Public/emoji/latest/emoji-test.txt"
OUT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "web-static", "emoji.json")
TONES = {chr(c) for c in range(0x1F3FB, 0x1F400)}


def build(text, max_version=99.0):
    groups, cur = [], None
    for line in text.splitlines():
        if line.startswith("# group:"):
            cur = {"g": line.split(":", 1)[1].strip(), "e": []}
            if cur["g"] != "Component": groups.append(cur)
            else: cur = None
            continue
        m = re.match(r"([0-9A-F ]+?)\s*; fully-qualified\s+# \S+ E([0-9.]+) ", line)
        if not m or cur is None or float(m.group(2)) > max_version: continue
        e = "".join(chr(int(c, 16)) for c in m.group(1).split())
        if not TONES & set(e): cur["e"].append(e)
    return groups


if __name__ == "__main__":
    mv = float(sys.argv[sys.argv.index("--max-version") + 1]) if "--max-version" in sys.argv else 99.0
    with tempfile.TemporaryDirectory() as d:  # the .txt is only a temp input; the json is what gets committed
        p = os.path.join(d, "emoji-test.txt")
        with urllib.request.urlopen(URL, timeout=60) as r, open(p, "wb") as f: f.write(r.read())
        with open(p, encoding="utf-8") as f: data = build(f.read(), mv)
    with open(OUT, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(OUT, sum(len(g["e"]) for g in data), "emoji,", len(data), "groups")
