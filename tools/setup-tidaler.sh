#!/bin/sh
# Install the Tidaler command-line tool into its own virtual environment.
#
# Tidaler 0.1.8 requires Python 3.12 or 3.13, while this project runs on 3.14, so it
# gets a separate venv. Nothing here touches Tidaler's own config or token; the CLI
# shares ~/.config/tidaler with the GUI app, so an existing login carries over.
set -eu
cd "$(dirname "$0")/.."
PY="${TIDALER_PYTHON:-/opt/homebrew/bin/python3.13}"
VERSION="${TIDALER_VERSION:-0.1.8}"
if [ ! -x "$PY" ]; then
  echo "Python 3.12/3.13 not found at $PY. Set TIDALER_PYTHON." >&2
  exit 2
fi
"$PY" -m venv .venv-tidaler
.venv-tidaler/bin/python -m pip install --quiet --upgrade pip
.venv-tidaler/bin/python -m pip install --quiet "tidaler==$VERSION"
printf 'tidaler installed: '
.venv-tidaler/bin/tidaler --version
echo "Binary: $(pwd)/.venv-tidaler/bin/tidaler"
