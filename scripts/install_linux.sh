#!/usr/bin/env bash
# Install Darkimiya for the current user on Linux.
#
# Installs into a private virtual environment rather than the system Python,
# puts a launcher on PATH, and registers a desktop entry so the application
# appears in the menu. Nothing is written outside the user's own directories,
# so no privilege escalation is required and uninstalling is a deletion.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
prefix=${PREFIX:-"$HOME/.local"}
venv_dir=${VENV_DIR:-"$prefix/share/darkimiya/venv"}
bin_dir="$prefix/bin"
desktop_dir="$prefix/share/applications"
icon_dir="$prefix/share/icons/hicolor/scalable/apps"

python_bin=${PYTHON_BIN:-python3}
if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "error: $python_bin is not on PATH" >&2
  exit 2
fi

version=$("$python_bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "error: Python 3.11 or newer is required, found $version" >&2
  exit 2
fi

echo "Installing Darkimiya into $venv_dir (Python $version)"
mkdir -p "$venv_dir" "$bin_dir" "$desktop_dir" "$icon_dir"
"$python_bin" -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade --quiet pip
"$venv_dir/bin/python" -m pip install --quiet "$project_dir"

# Fail here rather than at the first RAW preview: without rawpy every RAW is
# decoded by a separate process per photograph, and on Linux there is no
# fallback decoder at all.
if ! "$venv_dir/bin/python" -c 'import rawpy' 2>/dev/null; then
  echo "error: rawpy did not install; RAW previews would not work" >&2
  exit 3
fi

for command in darkimiya darkimiya-review; do
  cat > "$bin_dir/$command" <<EOF
#!/usr/bin/env bash
exec "$venv_dir/bin/$command" "\$@"
EOF
  chmod 755 "$bin_dir/$command"
done

install -m 644 "$project_dir/packaging/linux/darkimiya.desktop" \
  "$desktop_dir/darkimiya.desktop"
if [[ -f "$project_dir/assets/opencull-icon.svg" ]]; then
  install -m 644 "$project_dir/assets/opencull-icon.svg" \
    "$icon_dir/darkimiya.svg"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$desktop_dir" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$prefix/share/icons/hicolor" 2>/dev/null || true
fi

echo
echo "Installed:"
echo "  launcher       $bin_dir/darkimiya"
echo "  desktop entry  $desktop_dir/darkimiya.desktop"
echo

missing=()
command -v zenity >/dev/null 2>&1 || command -v kdialog >/dev/null 2>&1 \
  || missing+=("zenity or kdialog (folder and file choosers)")
command -v darktable-cli >/dev/null 2>&1 \
  || missing+=("darktable (optional: guided RAW development)")
if ((${#missing[@]})); then
  echo "Optional components not found:"
  printf '  - %s\n' "${missing[@]}"
  echo
fi

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "Note: $bin_dir is not on PATH; add it to use the darkimiya command." ;;
esac
