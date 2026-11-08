"""Interactive setup for a server without a client pack."""

from __future__ import annotations

import os
import re
import select
import shlex
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from . import runtime
from .builder import ModRecord, _find_game_directory, build
from .instance import ServerSpec, VERSION_TOKEN, instance_root
from .progress import ConsoleProgress

try:
    import termios
    import tty
except ImportError:
    termios = tty = None


INDENT = " " * 9


def _style(value: str, code: str) -> str:
    if (sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
            and "NO_COLOR" not in os.environ):
        return f"\033[{code}m{value}\033[0m"
    return value


def _badge(label: str, color: str = "45;97") -> str:
    return " " * max(0, 5 - len(label)) + _style(f" {label} ", color)


def _heading(question: str, label: str) -> None:
    print(f"\n{_badge(label)}  {question}")


@dataclass(frozen=True)
class ServerJar:
    url: str
    checksum: str
    algorithm: str


def _mojang_jar(details: dict) -> ServerJar | None:
    server = details.get("downloads", {}).get("server", {})
    if isinstance(server.get("url"), str) and isinstance(server.get("sha1"), str):
        return ServerJar(server["url"], server["sha1"], "sha1")
    return None


def _paper_jar(version: str, build_id: str | None = None) -> ServerJar | None:
    builds = runtime._get_json(
        f"https://fill.papermc.io/v3/projects/paper/versions/{quote(version, safe='')}/builds"
    )
    if not isinstance(builds, list):
        raise runtime.InstallError("Paper returned an invalid build list")
    for build in builds:
        if not isinstance(build, dict):
            continue
        matches = (build.get("channel") == "STABLE" if build_id is None
                   else str(build.get("id")) == build_id)
        if not matches:
            continue
        jar = build.get("downloads", {}).get("server:default", {})
        checksum = jar.get("checksums", {}).get("sha256")
        if isinstance(jar.get("url"), str) and isinstance(checksum, str):
            return ServerJar(jar["url"], checksum, "sha256")
    return None


def _purpur_jar(version: str, build_id: str = "latest") -> ServerJar | None:
    base = f"https://api.purpurmc.org/v2/purpur/{quote(version, safe='')}"
    build = runtime._get_json(f"{base}/{build_id}")
    if not isinstance(build, dict):
        raise runtime.InstallError("Purpur returned invalid build metadata")
    number = build.get("build")
    checksum = build.get("md5")
    if (build.get("result") == "SUCCESS" and isinstance(number, str)
            and number.isdecimal() and isinstance(checksum, str)):
        return ServerJar(f"{base}/{number}/download", checksum, "md5")
    return None


def _forge_version(version: str) -> str | None:
    # Older Forge installers don't generate the run.sh used by install_runtime.
    legacy = re.fullmatch(r"1\.(\d+)(?:\.\d+)?", version)
    if legacy and int(legacy[1]) < 17:
        return None
    return runtime._latest_forge_loader(version)


def _available_cores(version: str, server_type: str, details: dict) -> dict[str, ServerJar | str]:
    cores: dict[str, ServerJar | str | None] = {}
    if server_type == "Vanilla":
        cores["Vanilla (Mojang)"] = _mojang_jar(details)
        providers = {"Paper": _paper_jar, "Purpur": _purpur_jar}
    else:
        providers = {
            "Fabric": runtime._latest_fabric_loader,
            "NeoForge": runtime._latest_neoforge_loader,
            "Forge": _forge_version,
            "Quilt": runtime._latest_quilt_loader,
        }
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        pending = {name: pool.submit(fetch, version) for name, fetch in providers.items()}
        for name, future in pending.items():
            try:
                cores[name] = future.result()
            except runtime.InstallError as error:
                cause = error.__cause__
                if isinstance(cause, URLError) and (
                    not isinstance(cause, HTTPError) or cause.code not in {400, 404}
                ):
                    print(f"  Could not check {name}: {cause}", file=sys.stderr)
    return {name: core for name, core in cores.items() if core is not None}


def _choose(label: str, question: str, choices: list[str], *, initial: int = 0) -> str:
    _heading(question, label)
    if (termios is None or not sys.stdin.isatty() or not sys.stdout.isatty()
            or os.environ.get("TERM") == "dumb"):
        for index, choice in enumerate(choices, 1):
            print(f"{INDENT}{index}. {choice}")
        while True:
            answer = input(f"{INDENT}Select a number: ").strip()
            for index, choice in enumerate(choices, 1):
                if answer == str(index):
                    return choice
            print(f"{INDENT}Enter a number from 1 to {len(choices)}.")

    fd = sys.stdin.fileno()
    settings = termios.tcgetattr(fd)
    selected = initial
    width = shutil.get_terminal_size().columns
    try:
        tty.setcbreak(fd)
        print("\033[?25l", end="")
        while True:
            for index, choice in enumerate(choices):
                name = choice[:max(0, width - len(INDENT) - 3)]
                marker = _style("●", "32") if index == selected else _style("○", "2")
                print(f"{INDENT}{marker} {name if index == selected else _style(name, '2')}")
            hint = f"{INDENT}↑/↓ to move · Enter to select"
            print(_style(hint[:width - 1], "2"), end="", flush=True)
            key = os.read(fd, 1)
            if key == b"\x1b":
                sequence = key
                while len(sequence) < 3 and select.select([fd], [], [], 0.05)[0]:
                    byte = os.read(fd, 1)
                    if not byte:
                        break
                    sequence += byte
                key = sequence
            print(f"\r\033[{len(choices)}A\033[J", end="", flush=True)
            if key in (b"\r", b"\n"):
                print(f"{INDENT}{_style(choices[selected], '2')}")
                return choices[selected]
            if key in (b"", b"\x04", b"\x1b"):
                raise EOFError
            if key in (b"\x1b[A", b"\x1bOA"):
                selected = (selected - 1) % len(choices)
            if key in (b"\x1b[B", b"\x1bOB"):
                selected = (selected + 1) % len(choices)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)
        print("\033[?25h", end="", flush=True)


def _choose_output(parent: Path, default: str = "minecraft-server") -> Path:
    _heading(f"Choose server name [{default}]:", "name")
    while True:
        name = input(INDENT).strip() or default
        if name in {".", ".."} or "/" in name or "\\" in name or not name.isprintable():
            print(f"{INDENT}Enter a folder name without a path.")
            continue
        output = parent / name
        if output.exists() or output.is_symlink():
            print(f"{INDENT}That folder already exists. Choose another name.")
            continue
        return output


def _choose_version(default: str = "latest") -> tuple[str, dict]:
    _heading(f"Choose server version [{default}]:", "ver")
    while True:
        version = input(INDENT).strip() or default
        if version == "latest":
            version = runtime._latest_minecraft_version()
        try:
            return version, runtime._minecraft_details(version)
        except runtime.UnknownMinecraftVersion:
            print(f"{INDENT}{_style('Version does not exist!', '31')}")


def _confirm_eula() -> bool:
    _heading("Before downloading the server, read the Minecraft EULA:", "EULA")
    print(f"{INDENT}https://www.minecraft.net/en-us/eula")
    return input(f'{INDENT}Type exactly "I agree" to accept: ') == "I agree"


def _exact_core(core_name: str, minecraft: str, version: str,
                latest: ServerJar | str) -> ServerJar | str | None:
    if not VERSION_TOKEN.fullmatch(version):
        return None
    if core_name == "Vanilla (Mojang)":
        return latest if version == minecraft else None
    if core_name in {"Paper", "Purpur"}:
        if not version.isascii() or not version.isdecimal():
            return None
        fetch = _paper_jar if core_name == "Paper" else _purpur_jar
        return fetch(minecraft, version)
    loader = core_name.lower()
    if loader == "neoforge":
        versions = runtime._neoforge_versions(minecraft)
        if minecraft == "1.20.1" and not version.startswith("1.20.1-"):
            version = f"1.20.1-{version}"
        return version if version in versions else None
    if loader == "forge":
        url, _ = runtime._installer_url(loader, minecraft, version)
        checksum = runtime._get_bytes(url + ".sha1", 200)
        return version if re.search(rb"\b[0-9a-fA-F]{40}\b", checksum) else None
    base = runtime.FABRIC_META if loader == "fabric" else runtime.QUILT_META
    data = runtime._get_json(f"{base}/loader/{quote(minecraft, safe='')}/{quote(version, safe='')}")
    entry = data.get("loader") if isinstance(data, dict) else None
    return version if isinstance(entry, dict) and entry.get("version") == version else None


def _choose_core_version(name: str, minecraft: str, latest: ServerJar | str) -> ServerJar | str:
    _heading("Choose core version [latest]:", "ver")
    while True:
        version = input(INDENT).strip()
        if version in {"", "latest"} or version == latest:
            return latest
        try:
            selected = _exact_core(name, minecraft, version, latest)
        except runtime.InstallError as error:
            cause = error.__cause__
            if not isinstance(cause, HTTPError) or cause.code not in {400, 404}:
                raise
            selected = None
        if selected is not None:
            return selected
        print(f"{INDENT}{_style('Version does not exist!', '31')}")


class _DownloadDisplay:
    def __init__(self, display: ConsoleProgress, *, gradient: bool = False):
        self.display = display
        self.percent = -1
        self.redraw = display.is_tty and os.environ.get("TERM") != "dumb"
        self.gradient = gradient and self.redraw and "NO_COLOR" not in os.environ
        self.phase = 0
        self.lock = Lock()
        self.stop = Event()
        self.animation: Thread | None = None

    def start(self) -> None:
        self.display.message("")
        self.update(0)
        if self.gradient:
            self.animation = Thread(target=self._animate, daemon=True)
            self.animation.start()

    def _animate(self) -> None:
        while not self.stop.wait(0.09):
            with self.lock:
                self.phase = (self.phase + 1) % 24
                self._render()

    def _render(self, result: str | None = None) -> None:
        label = f"{self.percent:03d}%"
        badge = _badge(label)
        if self.gradient:
            cells = []
            for index, char in enumerate(f" {label} "):
                wave = 12 - abs(12 - (self.phase + index * 3) % 24)
                green = 167 + (255 - 167) * wave // 12
                blue = 33 + (51 - 33) * wave // 12
                cells.append(f"\033[30;48;2;0;{green};{blue}m{char}")
            badge = " " + "".join(cells) + "\033[0m"
        line = f"{badge}  {result or 'Downloading core...'}"
        if self.redraw:
            print(f"\r\033[2K{line}", end="\n" if result else "", flush=True)
        else:
            self.display.message(line)

    def update(self, percent: int, result: str | None = None) -> None:
        with self.lock:
            if result is None and percent <= self.percent:
                return
            self.percent = percent
            if self.redraw or percent == 0 or result is not None:
                self._render(result)

    def finish(self, success: bool) -> None:
        self.stop.set()
        if self.animation is not None:
            self.animation.join()
        self.update(100 if success else self.percent,
                    "Core downloaded." if success else "Core installation failed.")


def _created(output: Path) -> None:
    print(f"\n{_badge('end', '46;30')}  Minecraft server created!")
    print(f"\n Enter your server directory using {_style(f'cd {shlex.quote(str(output))}', '36')}")
    print(f" Run {_style('./start.sh', '36')} to start the server. CTRL + C to stop.")


def _run_new(parent: Path, java: str, display: ConsoleProgress) -> None:
    output = _choose_output(parent)
    version, details = _choose_version()
    server_type = _choose("type", "Choose server type:", ["Vanilla", "Mods"])
    cores = _available_cores(version, server_type, details)
    if not cores:
        raise runtime.InstallError(f"No {server_type.lower()} cores found for Minecraft {version}")
    core_name = _choose("core", "Choose server core:", list(cores))
    core = _choose_core_version(core_name, version, cores[core_name])
    if not _confirm_eula():
        raise ValueError("Minecraft EULA was not accepted; type exactly 'I agree' to continue")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.tmp-", dir=output.parent) as staging:
        # Keep the parent staging directory so TemporaryDirectory can clean up
        # after the server directory is moved into its final location.
        directory = Path(staging) / "server"
        directory.mkdir()
        download = _DownloadDisplay(display, gradient=True)
        download.start()
        installed = False
        try:
            if isinstance(core, ServerJar):
                required_java = runtime._minecraft_java_major(version, details)
                runtime.ensure_java(
                    java, required_java, directory,
                    progress=lambda done, total: download.update(min(45, done * 45 // total)),
                )
                runtime._download_checked(core.url, directory / "server.jar",
                                          core.checksum, core.algorithm,
                                          progress=lambda done, total: download.update(
                                              45 + min(50, done * 50 // total)
                                          ))
                download.update(95)
                runtime._write_start_script(core_name.lower(), directory)
            else:
                spec = ServerSpec(version, core_name.lower(), core, "init")
                runtime.install_runtime(spec, directory, java,
                                        minecraft_details=details, progress=download.update)
                (directory / "mods").mkdir(exist_ok=True)
            (directory / "eula.txt").write_text("eula=true\n", encoding="utf-8")
            if output.exists() or output.is_symlink():
                raise ValueError(f"Output already exists: {output}")
            directory.rename(output)
            installed = True
        finally:
            download.finish(installed)

    _created(output)


def _review_client_spec(spec: ServerSpec | None) -> ServerSpec:
    if spec is None:
        version, details = _choose_version()
        loader = None
        loader_version = None
    else:
        version, loader, loader_version = spec.minecraft_version, spec.loader, spec.loader_version
        details = None

    def choose_core() -> tuple[str, str]:
        nonlocal details
        if details is None:
            details = runtime._minecraft_details(version)
        cores = _available_cores(version, "Mods", details)
        if not cores:
            raise runtime.InstallError(f"No mod cores found for Minecraft {version}")
        name = _choose("core", "Choose server core:", list(cores))
        return name.lower(), cores[name]

    while True:
        choices = [
            f"Minecraft {version}",
            "Mods",
            " ".join(filter(None, [loader, loader_version])) or "Choose core",
            "Continue",
        ]
        choice = _choose("check", "Check the recognized data:", choices, initial=len(choices) - 1)
        if choice == choices[0]:
            new_version, details = _choose_version(version)
            if new_version != version:
                version, loader, loader_version = new_version, None, None
        elif choice == choices[1]:
            _choose("type", "Choose server type:", ["Mods"])
        elif choice == choices[2]:
            loader, loader_version = choose_core()
        else:
            if loader is None:
                loader, loader_version = choose_core()
            if spec and (version, loader, loader_version) == (
                spec.minecraft_version, spec.loader, spec.loader_version
            ):
                return spec
            return ServerSpec(version, loader, loader_version, "interactive wizard")


def _run_client(parent: Path, java: str, display: ConsoleProgress) -> None:
    _heading("Enter client instance path:", "dir")
    while True:
        value = input(INDENT).strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        if not value:
            continue
        client = Path(value).expanduser().resolve()
        try:
            _find_game_directory(client)
            break
        except ValueError as error:
            print(f"{INDENT}{_style(str(error), '31')}")
    output = _choose_output(parent, f"{instance_root(client).name}-server")

    statuses: dict[str, str] = {}
    stop = Event()
    animation = None
    download = _DownloadDisplay(display)
    installed = False
    reviewing_mods = False

    def animate() -> None:
        dots = 1
        while not stop.wait(0.25):
            dots = dots % 3 + 1
            print(f"\r\033[2K{_badge('mods')}  Checking mods{'.' * dots}", end="", flush=True)

    def stop_animation() -> None:
        if animation is not None and not stop.is_set():
            stop.set()
            animation.join()
            print()

    def mod_status(filename: str, _name: str, side: str) -> None:
        nonlocal animation
        if not statuses:
            print(f"\n{_badge('mods')}  Checking mods...", end="" if download.redraw else "\n",
                  flush=True)
            if download.redraw:
                animation = Thread(target=animate, daemon=True)
                animation.start()
        statuses[filename] = side

    def counts() -> None:
        print(f"{INDENT}Server-side: {list(statuses.values()).count('server')}")
        print(f"{INDENT}Client-side: {list(statuses.values()).count('client')}")

    def review_spec(spec: ServerSpec | None) -> ServerSpec:
        stop_animation()
        return _review_client_spec(spec)

    def resolve_mod(record: ModRecord) -> str:
        nonlocal reviewing_mods
        stop_animation()
        if not reviewing_mods:
            counts()
            reviewing_mods = True
        return _choose("err", f"Choose {record.display_name} side:", ["Server", "Client"]).lower()

    def confirm() -> bool:
        stop_animation()
        counts()
        return _confirm_eula()

    def install(spec: ServerSpec, directory: Path, java: str) -> dict:
        download.start()
        return runtime.install_runtime(spec, directory, java, progress=download.update)

    try:
        report = build(
            client, output, java=java, curseforge_api_key=os.environ.get("CURSEFORGE_API_KEY"),
            review_spec=review_spec, resolve_mod=resolve_mod, mod_status=mod_status,
            confirm_eula=confirm, runtime_installer=install,
        )
        installed = True
    finally:
        stop_animation()
        if download.percent >= 0:
            download.finish(installed)
    _created(output)
    for warning in report["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)


def run_wizard(parent: Path, java: str, display: ConsoleProgress) -> None:
    print(f"\n{_badge('srvcrt', '42;30')}  Minecraft server creator")
    source = _choose("src", "Choose source for server:", ["New", "Client"])
    if source == "Client":
        _run_client(parent, java, display)
    else:
        _run_new(parent, java, display)
