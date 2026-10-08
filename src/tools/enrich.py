#!/usr/bin/env python3
"""Generate video posters, voice waveforms and document previews for messages in archive.db
whose `enrich` flag is 0. Results go into the message's data JSON ("p", "wh", "w", "d", "pg").
Thumbnails live in $WA_WEB/thumbs (cache, never wiped). Safe to run repeatedly / concurrently with sync.
"""
import hashlib, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import waconf
from archive_db import Archive
from build_viewer import (IMG_DOC_EXT, compute_wave, doc_ext, jpeg_size, make_doc_thumb, make_thumb,
                          pdf_pages)

WAVE_CACHE = os.path.join(waconf.THUMBS, "waves.json")


def work(row):
    """Returns dict of data fields to set (may be empty)."""
    d = json.loads(row["data"]) if row["data"] else {}
    m = d.get("m")
    if not m:
        return {}
    src = os.path.join(waconf.STORE, m)
    if not os.path.exists(src):
        return {}
    name = hashlib.sha1(m.encode("utf-8")).hexdigest() + ".jpg"
    dst = os.path.join(waconf.THUMBS, name)
    out = {}
    k = row["kind"]
    if k == "vid":
        if make_thumb(src, dst):
            out["p"] = "thumbs/" + name
            if "wh" not in d:
                wh = jpeg_size(dst)
                if wh:
                    out["wh"] = list(wh)
    elif k == "ptt":
        st = os.stat(src)
        c = WAVES.get(m)
        if c and c[:2] == [st.st_size, int(st.st_mtime)]:
            res = (c[2], c[3])
        else:
            res = compute_wave(src)
            if res:
                WAVES[m] = [st.st_size, int(st.st_mtime), res[0], res[1]]
        if res:
            out["w"] = res[0]
            if not d.get("d"):
                out["d"] = res[1]
    elif k == "doc":
        ext = doc_ext(d)
        if ext == "pdf" or ext in IMG_DOC_EXT:
            if make_doc_thumb(src, dst, ext):
                out["p"] = "thumbs/" + name
                wh = jpeg_size(dst)
                if wh:
                    out["wh"] = list(wh)
            if ext == "pdf" and not d.get("pg"):
                pg = pdf_pages(src)
                if pg:
                    out["pg"] = pg
    return out


def main():
    global WAVES
    os.makedirs(waconf.THUMBS, exist_ok=True)
    try:
        WAVES = json.load(open(WAVE_CACHE, encoding="utf-8"))
    except (OSError, ValueError):
        WAVES = {}
    ar = Archive()
    rows = ar.db.execute("select id, kind, data from messages where enrich = 0").fetchall()
    if not rows:
        return
    t0, done = time.time(), 0
    print(f"enrich: {len(rows)} media messages", flush=True)
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for row, res in zip(rows, ex.map(work, rows)):
            if res:
                ar.set_data(row["id"], **res)
            ar.db.execute("update messages set enrich = 1 where id = ?", (row["id"],))
            done += 1
            if done % 2000 == 0:
                ar.commit()
                print(f"  {done}/{len(rows)} ({time.time() - t0:.0f}s)", flush=True)
    ar.commit()
    tmp = WAVE_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(WAVES, f, separators=(",", ":"))
    os.replace(tmp, WAVE_CACHE)
    print(f"enrich: done {done} in {time.time() - t0:.1f}s", flush=True)


WAVES = {}
if __name__ == "__main__":
    main()
