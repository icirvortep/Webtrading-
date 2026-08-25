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

# --- aktualizace na nejnovější verzi ---
# Uživatel nemá důvod psát git příkazy. Vlastní nastavení i .env s klíči
# musí update přežít, proto se konfigurace odloží stranou a pak vrátí.
update_repo() {
  git rev-parse --git-dir >/dev/null 2>&1 || return 0
  local branch backup local_head remote_head
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 0
  say "$BLUE" "Kontroluji aktualizace…"
  if ! git fetch --quiet origin "$branch" 2>/dev/null; then
    say "$YELLOW" "Aktualizace přeskočena (bez připojení) — pouštím, co je po ruce."
    return 0
  fi
  local_head="$(git rev-parse HEAD)"
  remote_head="$(git rev-parse "origin/$branch" 2>/dev/null)" || return 0
  if [ "$local_head" = "$remote_head" ]; then
    say "$GREEN" "Máš nejnovější verzi."
    return 0
  fi

  backup="$(mktemp)"
  [ -f config/config.yaml ] && cp config/config.yaml "$backup"
  # zahodíme jen změny ve sledovaných souborech; .env a data/ jsou mimo git
  git checkout --quiet -- . 2>/dev/null || true
  if git merge --ff-only --quiet "origin/$branch" 2>/dev/null; then
    say "$GREEN" "Aktualizováno na nejnovější verzi."
  elif git reset --hard --quiet "origin/$branch" 2>/dev/null; then
    say "$GREEN" "Aktualizováno (verze srovnána se serverem)."
  else
    say "$YELLOW" "Aktualizace se nezdařila — pokračuji s tím, co je nainstalováno."
  fi
  if [ -s "$backup" ]; then
    # složka může po aktualizaci chybět, pokud v ní nezůstal sledovaný soubor
    mkdir -p config && cp "$backup" config/config.yaml
    say "$GREEN" "Tvoje nastavení zachováno."
  fi
  rm -f "$backup"

  if ! python -c "import ccxt, fastapi" >/dev/null 2>&1; then
    pip install --quiet -r requirements.txt
  fi
}
[ "${ATB_SKIP_UPDATE:-}" = "1" ] || update_repo

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
