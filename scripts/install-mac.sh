#!/bin/bash
# Položí aplikaci "Adaptive Trading Bot" na plochu.
#
# Spusť z naklonované složky:   ./scripts/install-mac.sh
# Volitelně jiné umístění:      ./scripts/install-mac.sh ~/Applications
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_APP="$ROOT/macos/AdaptiveTradingBot.app"
APP_NAME="Adaptive Trading Bot.app"

BLUE=$'\033[34m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
say() { printf "%s%s%s\n" "$1" "$2" "$OFF"; }

if [ "$(uname -s)" != "Darwin" ]; then
  say "$RED" "Tenhle instalátor je pro macOS. Na jiném systému spusť rovnou ./start-mac.command"
  exit 1
fi
[ -d "$SOURCE_APP" ] || { say "$RED" "Chybí $SOURCE_APP — je repozitář kompletní?"; exit 1; }

# Cíl: plocha (i s iCloud Drive), nebo cesta zadaná argumentem.
if [ $# -ge 1 ]; then
  TARGET_DIR="$1"
else
  TARGET_DIR="$HOME/Desktop"
  [ -d "$TARGET_DIR" ] || TARGET_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Desktop"
  [ -d "$TARGET_DIR" ] || { say "$RED" "Plochu jsem nenašel — zadej cíl: ./scripts/install-mac.sh ~/Applications"; exit 1; }
fi
TARGET_APP="$TARGET_DIR/$APP_NAME"

say "$BLUE" "Instaluji do: $TARGET_DIR"
rm -rf "$TARGET_APP"
cp -R "$SOURCE_APP" "$TARGET_APP"

# Zapíšeme, kde bot bydlí, ať ho aplikace najde odkudkoli.
printf '%s' "$ROOT" > "$TARGET_APP/Contents/Resources/bot-home.txt"
chmod +x "$TARGET_APP/Contents/MacOS/AdaptiveTradingBot"

# Bez tohohle by macOS mohl hlásit, že je aplikace poškozená.
xattr -cr "$TARGET_APP" 2>/dev/null || true
# Donutí Finder načíst ikonu hned, ne až po restartu.
touch "$TARGET_APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$TARGET_APP" >/dev/null 2>&1 || true

say "$GREEN" "Hotovo — na ploše máš „Adaptive Trading Bot“."
echo
say "$BLUE"  "Dvojklik ji spustí. Poprvé si sama doinstaluje, co potřebuje,"
say "$BLUE"  "zeptá se na režim a otevře rozhraní v prohlížeči."
echo
say "$YELLOW" "Kdyby macOS hlásil „od neověřeného vývojáře“:"
say "$YELLOW" "  klikni na ikonu pravým tlačítkem → Otevřít → Otevřít."
