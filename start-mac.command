#!/bin/bash
# Spouštěč pro macOS — dvojklik v Finderu, nebo ./start-mac.command v Terminálu.
# Poprvé si sám vytvoří prostředí a nainstaluje závislosti.
set -euo pipefail
cd "$(dirname "$0")"

BLUE=$'\033[34m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
say() { printf "%s%s%s\n" "$1" "$2" "$OFF"; }

say "$BLUE" "=== Adaptive Trading Bot ==="

# --- Python ---
PY=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print(sys.version_info >= (3, 11))' 2>/dev/null || echo False)
    if [ "$version" = "True" ]; then PY="$candidate"; break; fi
  fi
done
if [ -z "$PY" ]; then
  say "$RED" "Chybí Python 3.11 nebo novější."
  say "$YELLOW" "Nainstaluj ho příkazem:  brew install python@3.12"
  say "$YELLOW" "(Homebrew: https://brew.sh)"
  read -r -p "Enter zavře okno…"; exit 1
fi
say "$GREEN" "Python: $($PY --version)"

# --- virtuální prostředí a závislosti ---
if [ ! -d .venv ]; then
  say "$BLUE" "Vytvářím prostředí (jednorázově, chvíli to potrvá)…"
  "$PY" -m venv .venv
fi
source .venv/bin/activate
if ! python -c "import ccxt, fastapi" >/dev/null 2>&1; then
  say "$BLUE" "Instaluji závislosti…"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
fi

# --- konfigurace ---
[ -f .env ] || { cp .env.example .env; say "$YELLOW" "Vytvořen soubor .env — API klíče doplň až budeš chtít obchodovat."; }
[ -f config/config.yaml ] || cp config/config.example.yaml config/config.yaml

# --- volba režimu ---
# Volbu jde předat i jako argument: 1/2/3 nebo přímo --offline/--live
MODE="${1:-}"
case "$MODE" in
  1|offline) MODE="--offline" ;;
  2|paper)   MODE="" ;;
  3|live)    MODE="--live" ;;
  ""|--offline|--live) ;;
  *) say "$RED" "Neznámý argument '$MODE' (použij 1/2/3 nebo --offline/--live)"; exit 1 ;;
esac
if [ -z "${1:-}" ]; then
  echo
  say "$BLUE" "V jakém režimu spustit?"
  echo "  1) OFFLINE  – simulovaná data, bez klíčů a bez internetu (na vyzkoušení)"
  echo "  2) PAPER    – reálná data z burzy, obchody jen nanečisto  [doporučeno]"
  echo "  3) LIVE     – skutečné peníze (vyžádá si potvrzení)"
  echo
  read -r -p "Volba [2]: " choice
  case "${choice:-2}" in
    1) MODE="--offline" ;;
    3) MODE="--live" ;;
    *) MODE="" ;;
  esac
fi

export PYTHONPATH=src
say "$GREEN" "Spouštím… rozhraní se otevře v prohlížeči."
echo
# shellcheck disable=SC2086
python -m atb run $MODE
