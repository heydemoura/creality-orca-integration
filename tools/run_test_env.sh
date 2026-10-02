#!/usr/bin/env bash
# Launch an OrcaSlicer nightly with the Creality CFS plugin pre-installed, in a SEPARATE data
# directory, so your normal OrcaSlicer configuration is never touched.
#
#   tools/run_test_env.sh [--appimage PATH] [--datadir PATH] [--source ~/.config/OrcaSlicer] [--host 192.168.0.128] [--fresh]
#
# What it does:
#   1. copies your OrcaSlicer config (presets, printers) into the test data dir (first run or --fresh)
#   2. installs plugin/creality_cfs.py into <datadir>/orca_plugins/creality_cfs/ (always refreshed)
#   3. points every user printer preset that has a print_host at the plugin agent ("creality_cfs")
#      and the "Creality Print" host type, with the given printer IP
#   4. removes the stale "last backup" pointer so no "restore unsaved items" prompt appears
#   5. runs the AppImage with --datadir <datadir>
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPIMAGE="${ORCA_NIGHTLY:-$HOME/AppImages/OrcaSlicer_nightly.AppImage}"
DATADIR="$HOME/.config/OrcaSlicer-cfs-test"
SOURCE="$HOME/.config/OrcaSlicer"
HOST=""
FRESH=0

while [ $# -gt 0 ]; do
  case "$1" in
    --appimage) APPIMAGE="$2"; shift 2 ;;
    --datadir)  DATADIR="$2"; shift 2 ;;
    --source)   SOURCE="$2"; shift 2 ;;
    --host)     HOST="$2"; shift 2 ;;
    --fresh)    FRESH=1; shift ;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

if [ ! -x "$APPIMAGE" ]; then
  echo "AppImage not found or not executable: $APPIMAGE" >&2
  echo "Download OrcaSlicer_Linux_AppImage_Ubuntu2404_nightly.AppImage from" >&2
  echo "https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/nightly-builds and chmod +x it," >&2
  echo "or pass --appimage PATH." >&2
  exit 1
fi

if [ "$FRESH" = 1 ] && [ -d "$DATADIR" ]; then
  echo "removing $DATADIR (--fresh)"
  rm -rf -- "$DATADIR"
fi

if [ ! -d "$DATADIR" ]; then
  mkdir -p "$DATADIR"
  for item in OrcaSlicer.conf user system printers; do
    [ -e "$SOURCE/$item" ] && cp -r "$SOURCE/$item" "$DATADIR/"
  done
  echo "copied config from $SOURCE to $DATADIR"
fi

mkdir -p "$DATADIR/orca_plugins/creality_cfs"
cp "$HERE/plugin/creality_cfs.py" "$DATADIR/orca_plugins/creality_cfs/creality_cfs.py"
if [ ! -f "$DATADIR/orca_plugins/creality_cfs/.install_state.json" ]; then
  cat > "$DATADIR/orca_plugins/creality_cfs/.install_state.json" <<'EOF'
{"installed_from": "local", "installed_version": "0.1.0", "plugin_name": "Creality CFS Sync", "enabled": true,
 "permissions": {"fs_read": [], "fs_readwrite": [], "network_http": [], "network_socket": [], "process": []},
 "capabilities": [{"Creality CFS (LAN)": true}, {"Creality CFS Manager": true}]}
EOF
fi
echo "plugin installed into $DATADIR/orca_plugins/creality_cfs"

python3 - "$DATADIR" "$HOST" <<'EOF'
import json, os, sys, glob
datadir, host = sys.argv[1], sys.argv[2]
conf = os.path.join(datadir, "OrcaSlicer.conf")
if os.path.isfile(conf):
    d = json.load(open(conf))
    if isinstance(d.get("app"), dict) and d["app"].pop("last_backup_path", None) is not None:
        json.dump(d, open(conf, "w"), indent="\t")
        print("removed stale last_backup_path from OrcaSlicer.conf")
changed = 0
for path in glob.glob(os.path.join(datadir, "user", "*", "machine", "*.json")):
    m = json.load(open(path))
    if "print_host" not in m and not host:
        continue
    if host:
        m["print_host"] = host
    m["host_type"] = "crealityprint"
    m["printer_agent"] = "creality_cfs"
    json.dump(m, open(path, "w"), indent="\t")
    changed += 1
    print(f"preset '{m.get('name')}' -> host {m.get('print_host')}, agent creality_cfs")
if not changed:
    print("no user printer preset with a print_host found; set Hostname/IP and Printer Agent in the GUI")
EOF

echo "starting $APPIMAGE --datadir $DATADIR"
echo "plugin log: $DATADIR/log/python_*.log"
exec env CREALITY_CFS_AUTO_OPEN="${CREALITY_CFS_AUTO_OPEN:-0}" "$APPIMAGE" --datadir "$DATADIR" "$@"
