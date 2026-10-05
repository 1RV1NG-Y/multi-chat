#!/usr/bin/env python3
"""Open multi-chat in its own window, starting the server in the background if it isn't running.

    launch.py              start the server if needed, then open the app window
    launch.py --stop       stop the background server
    launch.py --install    add multi-chat to your desktop's app menu (Linux, freedesktop)
    launch.py --uninstall  remove it again

The window is your default browser's "app" mode (--app=URL) when it's Chromium-based
(Brave, Chrome, Chromium, Vivaldi, Edge, ...), otherwise a normal browser window. The
browser is started with the same command as its own menu entry, so any flags you've
customised there (extensions, profiles) still apply. Set MULTICHAT_BROWSER to override,
e.g. MULTICHAT_BROWSER="brave --app=%s".
"""
import configparser
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"
PORT = int(os.environ.get("MULTICHAT_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "multi-chat"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
DESKTOP_FILE = DATA_HOME / "applications" / "multi-chat.desktop"
CHROMIUM_LIKE = re.compile(r"brave|chrom|thorium|helium|vivaldi|edge|opera|cromite", re.I)


def running():
    try:
        with urllib.request.urlopen(f"{URL}/api/info", timeout=1):
            return True
    except OSError:
        return False


def notify(msg):
    print(msg, file=sys.stderr)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "multi-chat", "multi-chat", msg], check=False)


def start_server():
    STATE.mkdir(parents=True, exist_ok=True)
    log = STATE / "server.log"
    env = dict(os.environ)
    # App launchers don't always get the login shell's PATH; claude usually lives in ~/.local/bin.
    local_bin = str(Path.home() / ".local/bin")
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '/usr/bin')}"
    with open(log, "ab") as out:
        subprocess.Popen([sys.executable, str(SERVER), "--port", str(PORT)], cwd=ROOT, env=env,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(100):
        if running():
            return True
        time.sleep(0.2)
    notify(f"The server didn't start. See {log}")
    return False


def desktop_dirs():
    yield DATA_HOME / "applications"
    for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"):
        yield Path(d) / "applications"


def default_browser_command():
    """The default browser's own launch command, without its %U-style placeholders."""
    try:
        desktop_id = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True,
                                    text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    for d in desktop_dirs():
        f = d / desktop_id
        if not f.is_file():
            continue
        entry = configparser.ConfigParser(interpolation=None, strict=False)
        entry.read(f, encoding="utf-8")
        exec_line = entry.get("Desktop Entry", "Exec", fallback="")
        # Drop placeholders (%U, flatpak's @@u ... @@) and the flag that only makes sense with them.
        args = [a for a in shlex.split(exec_line) if not a.startswith(("%", "@@")) and a != "--file-forwarding"]
        return args or None
    return None


def open_window():
    override = os.environ.get("MULTICHAT_BROWSER")
    if override:
        cmd = shlex.split(override % URL if "%s" in override else f"{override} {URL}")
    else:
        browser = default_browser_command()
        if browser and CHROMIUM_LIKE.search(" ".join(browser)):
            cmd = browser + [f"--app={URL}"]
        elif browser:
            cmd = browser + [URL]
        else:
            cmd = ["xdg-open", URL]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def stop():
    # SIGINT, not SIGTERM: the server treats it like Ctrl+C and stops any running CLI turns on the way out.
    found = subprocess.run(["pkill", "-INT", "-f", str(SERVER)], check=False).returncode == 0
    notify("Server stopped." if found else "The server wasn't running.")


def window_class():
    """Chromium names --app windows "<browser>-<host>__-<profile>"; matching it groups the window under our icon."""
    browser = " ".join(default_browser_command() or [])
    name = next((n for n in ("brave", "chromium", "chrome", "vivaldi", "msedge") if n in browser.lower()), None)
    return f"StartupWMClass={name}-127.0.0.1__-Default\n" if name and not os.environ.get("MULTICHAT_BROWSER") else ""


def install():
    launcher = Path(__file__).resolve()
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(f"""[Desktop Entry]
Type=Application
Name=multi-chat
GenericName=AI group chat
Comment=Chat with Claude Code and Codex together
Exec={shlex.quote(sys.executable)} {shlex.quote(str(launcher))}
Icon={ROOT / "static" / "icon.svg"}
Terminal=false
Categories=Utility;
Keywords=claude;codex;ai;chat;
{window_class()}Actions=stop;

[Desktop Action stop]
Name=Stop server
Exec={shlex.quote(sys.executable)} {shlex.quote(str(launcher))} --stop
""")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(DESKTOP_FILE.parent)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Installed {DESKTOP_FILE}. Look for \"multi-chat\" in your app menu.")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--stop":
        return stop()
    if arg == "--install":
        return install()
    if arg == "--uninstall":
        DESKTOP_FILE.unlink(missing_ok=True)
        return print(f"Removed {DESKTOP_FILE}.")
    if arg:
        sys.exit(__doc__)
    if running() or start_server():
        open_window()


if __name__ == "__main__":
    main()
