#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ACTION=${1:-}
if [ -z "$ACTION" ]; then
  echo "usage: run_sandboxed.sh ACTION [arguments...]" >&2
  exit 64
fi
shift

case "$ACTION" in
  check-environment) MODE=offline; SCRIPT=check_environment.py ;;
  rank-releases) MODE=offline; SCRIPT=rank_releases.py ;;
  select-payload) MODE=offline; SCRIPT=select_payload.py ;;
  check-subtitles) MODE=offline; SCRIPT=check_subtitles.py ;;
  plan-library) MODE=offline; SCRIPT=plan_library.py ;;
  write-nfo) MODE=library; SCRIPT=write_nfo.py ;;
  clean-clutter) MODE=library; SCRIPT=clean_clutter.py ;;
  refresh-checksums) MODE=library; SCRIPT=refresh_checksums.py ;;
  probe-ext) MODE=network-readonly; SCRIPT=probe_ext.py ;;
  qbt) MODE=qbt; SCRIPT=qbittorrent_api.py ;;
  monitor-download) MODE=qbt; SCRIPT=monitor_download.py ;;
  prepare-download) MODE=qbt; SCRIPT=prepare_download.py ;;
  remux-mkv) MODE=staging; SCRIPT=remux_mkv.py ;;
  *) echo "unknown sandbox action: $ACTION" >&2; exit 64 ;;
esac

export PYTHONDONTWRITEBYTECODE=1
MOVIES_ROOT=${MOVIES_NERD_MOVIES_ROOT:-"$HOME/Documents/Movies"}
SERIES_ROOT=${MOVIES_NERD_SERIES_ROOT:-"$HOME/Documents/Series"}
MOVIE_STAGE="$MOVIES_ROOT/.incoming/Movies Nerd"
SERIES_STAGE="$SERIES_ROOT/.incoming/Movies Nerd"
if command -v sandbox-exec >/dev/null 2>&1; then
  if ! sandbox-exec -p '(version 1) (allow default)' /usr/bin/true >/dev/null 2>&1; then
    echo "error: sandbox-exec exists but cannot create a sandbox in this environment" >&2
    exit 77
  fi
  exec sandbox-exec -D MOVIES_ROOT="$MOVIES_ROOT" -D SERIES_ROOT="$SERIES_ROOT" -D MOVIE_STAGE="$MOVIE_STAGE" -D SERIES_STAGE="$SERIES_STAGE" -f "$SCRIPT_DIR/sandbox/$MODE.sb" python3 "$SCRIPT_DIR/$SCRIPT" "$@"
fi

echo "warning: sandbox-exec unavailable; using script-level safety checks only" >&2
exec python3 "$SCRIPT_DIR/$SCRIPT" "$@"
