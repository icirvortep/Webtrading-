"""Test samoaktualizace ve spouštěči.

Reprodukuje přesně situaci, kvůli které `git pull` uživateli selhával:
sledovaný config/config.yaml s vlastními úpravami, který nová verze
z gitu odstraňuje. Aktualizace musí projít, nastavení i .env přežít.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "start-mac.command"


def git(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env={"HOME": str(cwd), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def extract_update_function() -> str:
    """Vytáhne update_repo ze spouštěče, ať se testuje opravdu ten kód."""
    lines = LAUNCHER.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("update_repo() {"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    body = "\n".join(lines[start:end + 1])
    return ("#!/bin/bash\nBLUE=''; GREEN=''; YELLOW=''; OFF=''\n"
            "say() { printf '%s\\n' \"$2\"; }\npython() { return 0; }\n"
            f"{body}\nupdate_repo\n")


@pytest.fixture()
def repos(tmp_path):
    """Vzdálený repozitář s novou verzí + lokální klon se starou."""
    remote = tmp_path / "remote"
    remote.mkdir()
    git("init", "-q", "--bare", "-b", "main", ".", cwd=remote)

    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", ".", cwd=work)
    git("remote", "add", "origin", str(remote), cwd=work)
    (work / "config").mkdir()
    (work / "config" / "config.yaml").write_text("quote: USDT\n", encoding="utf-8")
    (work / "code.py").write_text("stara verze\n", encoding="utf-8")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "v1", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)

    local = tmp_path / "local"
    git("clone", "-q", str(remote), str(local), cwd=tmp_path)

    # nová verze přestane config verzovat a změní kód
    git("rm", "-q", "--cached", "config/config.yaml", cwd=work)
    (work / ".gitignore").write_text("config/config.yaml\n", encoding="utf-8")
    (work / "code.py").write_text("nova verze\n", encoding="utf-8")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "v2", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return local


def run_update(local: Path) -> str:
    script = local / "_update.sh"
    script.write_text(extract_update_function(), encoding="utf-8")
    result = subprocess.run(
        ["bash", str(script)], cwd=local, capture_output=True, text=True,
        env={"HOME": str(local), "PATH": "/usr/bin:/bin",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    script.unlink()
    return result.stdout + result.stderr


def test_update_succeeds_despite_the_conflicting_config(repos):
    """Tohle je přesně stav, ve kterém `git pull` hlásil 'would be overwritten'."""
    (repos / "config" / "config.yaml").write_text("quote: USDC\n", encoding="utf-8")
    output = run_update(repos)
    assert "Aktualizováno" in output
    assert (repos / "code.py").read_text(encoding="utf-8").strip() == "nova verze"


def test_user_settings_survive_the_update(repos):
    (repos / "config" / "config.yaml").write_text("quote: USDC\n", encoding="utf-8")
    run_update(repos)
    assert (repos / "config" / "config.yaml").read_text(encoding="utf-8") == "quote: USDC\n"


def test_api_keys_are_never_touched(repos):
    """.env je mimo git — aktualizace ho nesmí smazat ani přepsat."""
    (repos / ".env").write_text("EXCHANGE_API_KEY=tajne\n", encoding="utf-8")
    (repos / "config" / "config.yaml").write_text("quote: USDC\n", encoding="utf-8")
    run_update(repos)
    assert (repos / ".env").read_text(encoding="utf-8") == "EXCHANGE_API_KEY=tajne\n"


def test_local_data_directory_is_preserved(repos):
    """Historie obchodů v data/ nesmí zmizet při aktualizaci."""
    data = repos / "data"
    data.mkdir()
    (data / "atb.sqlite").write_text("historie", encoding="utf-8")
    run_update(repos)
    assert (data / "atb.sqlite").read_text(encoding="utf-8") == "historie"


def test_second_run_reports_up_to_date(repos):
    run_update(repos)
    assert "nejnovější" in run_update(repos)


def test_missing_config_directory_is_recreated(repos):
    """Když aktualizace složku smaže, musí se nastavení přesto vrátit."""
    (repos / "config" / "config.yaml").write_text("quote: USDC\n", encoding="utf-8")
    output = run_update(repos)
    assert (repos / "config").is_dir()
    assert "zachováno" in output


def test_update_is_skipped_outside_a_git_checkout(tmp_path):
    """Stažená kopie bez gitu nesmí spadnout, jen se přeskočí."""
    plain = tmp_path / "plain"
    plain.mkdir()
    script = plain / "_update.sh"
    script.write_text(extract_update_function(), encoding="utf-8")
    result = subprocess.run(["bash", str(script)], cwd=plain, capture_output=True,
                            text=True, env={"HOME": str(plain), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0


def test_launcher_allows_disabling_the_update():
    assert "ATB_SKIP_UPDATE" in LAUNCHER.read_text(encoding="utf-8")


def test_update_never_runs_git_clean():
    """git clean by smazal .env s klíči — nesmí se ve spouštěči objevit."""
    assert "git clean" not in LAUNCHER.read_text(encoding="utf-8")


# ---------- přenos API klíčů mezi instalacemi ----------

def extract_env_migration() -> str:
    """Vytáhne ze spouštěče část, která hledá klíče v předchozích instalacích."""
    text = LAUNCHER.read_text(encoding="utf-8")
    start = text.index("has_keys() {")
    # značka musí být jednoznačná — podobný řádek je i v aktualizaci výš
    end = text.index("[ -f config/config.yaml ] || cp config/config.example.yaml")
    return ("#!/bin/bash\nGREEN=''; YELLOW=''\nsay() { printf '%s\\n' \"$2\"; }\n"
            + text[start:end])


def run_migration(cwd: Path, home: Path) -> str:
    script = cwd / "_migrate.sh"
    script.write_text(extract_env_migration(), encoding="utf-8")
    result = subprocess.run(
        ["bash", str(script)], cwd=cwd, capture_output=True, text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    script.unlink()
    return result.stdout + result.stderr


@pytest.fixture()
def fresh_install(tmp_path):
    """Čerstvě stažená kopie bez .env, plus domovský adresář."""
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Documents").mkdir()
    install = home / "Desktop" / "Adaptive Trading" / "bot"
    install.mkdir(parents=True)
    (install / ".env.example").write_text(
        "EXCHANGE_API_KEY=\nEXCHANGE_API_SECRET=\n", encoding="utf-8")
    return install, home


def test_keys_are_carried_over_from_the_previous_install(fresh_install):
    """Přenesení projektu jinam nesmí znamenat opisování klíčů z burzy."""
    install, home = fresh_install
    old = home / "AdaptiveTradingBot"
    old.mkdir()
    (old / ".env").write_text(
        "EXCHANGE_API_KEY=klic123\nEXCHANGE_API_SECRET=secret456\n", encoding="utf-8")

    output = run_migration(install, home)
    assert "přenesl" in output
    assert "klic123" in (install / ".env").read_text(encoding="utf-8")


def test_existing_keys_are_never_overwritten(fresh_install):
    install, home = fresh_install
    (install / ".env").write_text("EXCHANGE_API_KEY=uz_tady_je\n", encoding="utf-8")
    old = home / "AdaptiveTradingBot"
    old.mkdir()
    (old / ".env").write_text("EXCHANGE_API_KEY=stary\n", encoding="utf-8")

    run_migration(install, home)
    assert "uz_tady_je" in (install / ".env").read_text(encoding="utf-8")


def test_empty_previous_keys_are_not_migrated(fresh_install):
    """Prázdná šablona se nesmí tvářit jako nalezené klíče."""
    install, home = fresh_install
    old = home / "AdaptiveTradingBot"
    old.mkdir()
    (old / ".env").write_text("EXCHANGE_API_KEY=\nEXCHANGE_API_SECRET=\n", encoding="utf-8")

    output = run_migration(install, home)
    assert "nejsou vyplněné" in output


def test_missing_previous_install_is_explained_not_fatal(fresh_install):
    install, home = fresh_install
    output = run_migration(install, home)
    assert "nejsou vyplněné" in output
    assert (install / ".env").exists()          # šablona se přesto vytvoří
