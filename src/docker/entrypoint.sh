#!/bin/sh
# Dispatch: sync | viewer | link | import [args] | pin | clearpin | shell.  The profile folder (profiles/<name>) is /data (WA_HOME).
set -e
cd /app
[ -f "$WA_HOME/wa.conf" ] || { echo "no wa.conf in $WA_HOME: create the profile with 'wa new <name>' and mount it at /data" >&2; exit 2; }
mkdir -p "$WA_HOME/state" && chmod 700 "$WA_HOME/state" 2>/dev/null || true
cmd="${1:-viewer}"; [ $# -gt 0 ] && shift
case "$cmd" in sync|viewer)  # empty archive.db on first start (the viewer only serves from the db if it exists)
  python3 -c 'import sys; sys.path.insert(0, "tools"); import archive_db; archive_db.Archive()' ;; esac
case "$cmd" in
  sync)     exec node tools/wa-sync/sync.mjs "$@" ;;
  link)     exec node tools/wa-sync/sync.mjs --pair ;;
  viewer)   exec python3 tools/wa-archive --no-open "$@" ;;
  import)   [ $# -gt 0 ] || set -- --latest; exec python3 tools/import_msgstore.py "$@" ;;
  pin)      exec python3 tools/wa-archive --set-pin ;;
  clearpin) exec python3 tools/wa-archive --clear-pin ;;
  shell)    exec bash "$@" ;;
  *)        echo "usage: sync|link|viewer|import [args]|pin|clearpin|shell" >&2; exit 1 ;;
esac
