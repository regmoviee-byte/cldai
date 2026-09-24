#!/bin/sh
# Builds dist/aihub.exe: a Windows launcher with a private Python runtime and aihub inside.
# Works on Linux/macOS/Windows (Git Bash). Needs: go, curl, python 3.
#   PY_VERSION=3.13.15 ./launcher/build.sh
set -eu

cd "$(dirname "$0")"
PY_VERSION="${PY_VERSION:-3.13.15}"
APP_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../aihub/__init__.py)"
PY=""
for c in python3 python; do  # skip the Microsoft Store "python3" stub, which doesn't run
  if "$c" -c "import sys" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "python 3 not found" >&2; exit 1; }
# Relative work dir so the same paths work for native Windows python under Git Bash.
WORK=.build
rm -rf "$WORK" && mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

echo "==> Python $PY_VERSION for Windows (official python.org build, via NuGet)"
curl -fsSL -o "$WORK/python.nupkg" \
  "https://api.nuget.org/v3-flatcontainer/python/$PY_VERSION/python.$PY_VERSION.nupkg"
"$PY" -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); z.extractall(sys.argv[2], [n for n in z.namelist() if n.startswith('tools/')])" "$WORK/python.nupkg" "$WORK/pkg"
RT="$WORK/pkg/tools"

echo "==> trimming runtime and adding aihub $APP_VERSION"
rm -rf "$RT/include" "$RT/libs" "$RT/Lib/ensurepip" "$RT/Lib/pydoc_data" "$RT"/Lib/site-packages/pip*
mkdir -p "$RT/Lib/site-packages"
cp -R ../aihub "$RT/Lib/site-packages/"
find "$RT" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -f payload.zip
"$PY" -c "import shutil, sys; shutil.make_archive('payload', 'zip', root_dir=sys.argv[1])" "$RT"

echo "==> icon and version resources"
export GOFLAGS=-mod=mod GOSUMDB=off
go run github.com/tc-hib/go-winres@v0.3.3 simply \
  --arch amd64 --icon icon.png --manifest cli \
  --product-name aihub --file-description "aihub — Claude ⇄ Codex" \
  --product-version "$APP_VERSION.0" --file-version "$APP_VERSION.0" \
  --original-filename aihub.exe --copyright "MIT"

echo "==> go build"
mkdir -p ../dist
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o ../dist/aihub.exe .
rm -f payload.zip rsrc_windows_*.syso
ls -lh ../dist/aihub.exe
