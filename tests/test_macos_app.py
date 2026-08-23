"""Testy balíčku aplikace pro macOS.

Spouštěč se testuje tak, že se v jeho kopii nahradí volání `osascript`
za `echo` — díky tomu jde ověřit, že najde správnou složku s botem,
i když tenhle stroj macOS není.
"""
import plistlib
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "macos" / "AdaptiveTradingBot.app"
LAUNCHER = APP / "Contents" / "MacOS" / "AdaptiveTradingBot"


def test_bundle_has_required_layout():
    assert (APP / "Contents" / "Info.plist").is_file()
    assert LAUNCHER.is_file()
    assert (APP / "Contents" / "Resources" / "AppIcon.icns").is_file()


def test_launcher_is_executable():
    """Bez práva ke spuštění by macOS aplikaci vůbec neotevřel."""
    assert LAUNCHER.stat().st_mode & 0o111, "spouštěči chybí příznak spustitelnosti"


def test_info_plist_is_valid_and_consistent():
    data = plistlib.loads((APP / "Contents" / "Info.plist").read_bytes())
    assert data["CFBundlePackageType"] == "APPL"
    assert data["CFBundleExecutable"] == LAUNCHER.name
    assert data["CFBundleIconFile"] == "AppIcon"
    assert data["CFBundleIdentifier"].count(".") >= 2
    assert data["NSHighResolutionCapable"] is True


def test_icon_is_a_valid_icns_with_retina_sizes():
    raw = (APP / "Contents" / "Resources" / "AppIcon.icns").read_bytes()
    assert raw[:4] == b"icns"
    assert struct.unpack(">I", raw[4:8])[0] == len(raw), "hlavička neodpovídá délce souboru"

    offset, types = 8, []
    while offset < len(raw):
        icon_type = raw[offset:offset + 4]
        length = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
        assert raw[offset + 8:offset + 12] == b"\x89PNG", f"{icon_type} není PNG"
        types.append(icon_type)
        offset += length
    assert offset == len(raw)
    for required in (b"ic07", b"ic08", b"ic09", b"ic10"):
        assert required in types, f"chybí velikost ikony {required.decode()}"


def test_launcher_syntax_is_valid():
    assert subprocess.run(["bash", "-n", str(LAUNCHER)], capture_output=True).returncode == 0


def _run_launcher(tmp_path: Path, bundle_parent: Path, home: Path) -> str:
    """Spustí kopii spouštěče s osascript nahrazeným za echo."""
    bundle = bundle_parent / "Adaptive Trading Bot.app"
    shutil.copytree(APP, bundle)
    script = bundle / "Contents" / "MacOS" / "AdaptiveTradingBot"
    # `cat` vypíše heredoc s AppleScriptem, takže je v něm vidět nalezená cesta
    patched = script.read_text(encoding="utf-8").replace("/usr/bin/osascript", "cat")
    script.write_text(patched, encoding="utf-8")
    script.chmod(0o755)

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    return result.stdout + result.stderr


@pytest.fixture()
def fake_bot(tmp_path: Path) -> Path:
    """Složka, která vypadá jako naklonovaný repozitář."""
    home = tmp_path / "home"
    bot = home / "AdaptiveTradingBot"
    bot.mkdir(parents=True)
    (bot / "start-mac.command").write_text("#!/bin/bash\necho ahoj\n", encoding="utf-8")
    return home


def test_launcher_finds_bot_via_written_path(tmp_path, fake_bot):
    """Instalátor zapíše cestu do balíčku — ta má nejvyšší prioritu."""
    elsewhere = tmp_path / "jinde"
    elsewhere.mkdir()
    (elsewhere / "start-mac.command").write_text("#!/bin/bash\n", encoding="utf-8")

    bundle_parent = tmp_path / "plocha"
    bundle_parent.mkdir()
    bundle = bundle_parent / "Adaptive Trading Bot.app"
    shutil.copytree(APP, bundle)
    (bundle / "Contents" / "Resources" / "bot-home.txt").write_text(
        str(elsewhere), encoding="utf-8")
    script = bundle / "Contents" / "MacOS" / "AdaptiveTradingBot"
    script.write_text(script.read_text(encoding="utf-8").replace("/usr/bin/osascript", "cat"),
                      encoding="utf-8")
    script.chmod(0o755)

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={"HOME": str(fake_bot), "PATH": "/usr/bin:/bin"},
    )
    output = result.stdout + result.stderr
    assert str(elsewhere) in output


def test_launcher_falls_back_to_home_directory(tmp_path, fake_bot):
    """Bez zapsané cesty najde bota v ~/AdaptiveTradingBot."""
    bundle_parent = tmp_path / "plocha"
    bundle_parent.mkdir()
    output = _run_launcher(tmp_path, bundle_parent, fake_bot)
    assert str(fake_bot / "AdaptiveTradingBot") in output


def test_launcher_finds_repo_when_app_sits_inside_it(tmp_path):
    """Aplikace spuštěná přímo z repozitáře (macos/…app) najde kořen sama."""
    home = tmp_path / "prazdny_domov"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / "macos").mkdir(parents=True)
    (repo / "start-mac.command").write_text("#!/bin/bash\n", encoding="utf-8")
    output = _run_launcher(tmp_path, repo / "macos", home)
    assert str(repo) in output


def test_launcher_explains_itself_when_nothing_is_found(tmp_path):
    home = tmp_path / "prazdny_domov"
    home.mkdir()
    bundle_parent = tmp_path / "nekde"
    bundle_parent.mkdir()
    output = _run_launcher(tmp_path, bundle_parent, home)
    assert "git clone" in output          # dialog radí, jak to spravit


def test_installer_refuses_non_macos():
    result = subprocess.run([str(ROOT / "scripts" / "install-mac.sh")],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "macOS" in result.stdout
