"""Install a complete server core from official loader and Mojang sources."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from . import __version__
from .instance import ServerSpec


USER_AGENT = f"srvcrt/{__version__} (Minecraft server modpack builder)"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2/versions"
QUILT_META = "https://meta.quiltmc.org/v3/versions"
FORGE_PROMOTIONS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEOFORGE_METADATA = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
)
NEOFORGE_LEGACY_METADATA = (
    "https://maven.neoforged.net/releases/net/neoforged/forge/maven-metadata.xml"
)
ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest"
AZUL_API = "https://api.azul.com/metadata/v1/zulu/packages/"


class InstallError(Exception):
    pass


class UnknownMinecraftVersion(InstallError):
    pass


def _open(url: str, timeout: int = 30):
    if not url.startswith("https://"):
        raise InstallError(f"Refusing non-HTTPS download: {url}")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
        except HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise InstallError(f"Cannot fetch {url}: {error}") from error
            last_error = error
        except (URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt == 2:
                raise InstallError(f"Cannot fetch {url}: {error}") from error
    raise InstallError(f"Cannot fetch {url}: {last_error}")


def _get_bytes(url: str, limit: int = 8_000_000) -> bytes:
    with _open(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise InstallError(f"Metadata response is too large: {url}")
    return data


def _get_json(url: str) -> object:
    try:
        return json.loads(_get_bytes(url))
    except (ValueError, UnicodeError) as error:
        raise InstallError(f"Invalid JSON from {url}: {error}") from error


def _java_major(java: str) -> int:
    try:
        result = subprocess.run([java, "-version"], capture_output=True,
                                text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"Java is not available for installing the server: {error}") from error
    text = result.stderr + result.stdout
    match = re.search(r'(?:version\s+)?"(\d+(?:\.\d+)*)', text)
    if result.returncode or not match:
        raise InstallError(f"Cannot determine Java version: {text.strip()[:300]}")
    major = int(match.group(1).split(".")[0])
    return int(match.group(1).split(".")[1]) if major == 1 else major


def _minecraft_manifest() -> dict:
    manifest = _get_json(MOJANG_MANIFEST)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("versions"), list):
        raise InstallError("Mojang returned an invalid version manifest")
    return manifest


def _latest_minecraft_version() -> str:
    latest = _minecraft_manifest().get("latest")
    release = latest.get("release") if isinstance(latest, dict) else None
    if not isinstance(release, str):
        raise InstallError("Mojang did not provide the latest release")
    return release


def _minecraft_details(minecraft_version: str) -> dict:
    manifest = _minecraft_manifest()
    version = next((item for item in manifest["versions"]
                    if isinstance(item, dict) and item.get("id") == minecraft_version), None)
    if not version:
        raise UnknownMinecraftVersion(f"Minecraft version {minecraft_version} was not found")
    if not isinstance(version.get("url"), str):
        raise InstallError("Mojang returned an invalid version download URL")
    details = _get_json(version["url"])
    if not isinstance(details, dict):
        raise InstallError("Mojang returned invalid Minecraft version metadata")
    return details


def _minecraft_java_major(minecraft_version: str, details: dict) -> int:
    java_version = details.get("javaVersion")
    major = java_version.get("majorVersion") if isinstance(java_version, dict) else None
    if isinstance(major, int):
        return major
    legacy = re.match(r"1\.(\d+)(?:\.(\d+))?(?:\D|$)", minecraft_version)
    if legacy:
        # Fallback when version metadata omits the javaVersion field.
        minor = int(legacy[1])
        patch = int(legacy[2]) if legacy[2] is not None else 0
        if minor <= 16:
            return 8
        if minor == 17:
            return 16
        if minor <= 19 or (minor == 20 and patch <= 4):
            return 17
        return 21
    modern = re.match(r"(\d+)\.(\d+)", minecraft_version)
    if modern and int(modern[1]) >= 26:
        return 25
    raise InstallError("Mojang did not provide the required Java version")



def _download_checked(url: str, destination: Path, expected: str,
                      algorithm: str = "sha1", *,
                      progress: Callable[[int, int], None] | None = None) -> str:
    digest = hashlib.new(algorithm)
    if not re.fullmatch(rf"[0-9a-fA-F]{{{digest.digest_size * 2}}}", expected):
        raise InstallError(f"No valid {algorithm.upper()} checksum for {url}")
    expected = expected.lower()
    with _open(url, timeout=60) as response, destination.open("wb") as stream:
        length = response.headers.get("Content-Length", "") if progress else ""
        total = int(length) if length.isascii() and length.isdecimal() else 0
        downloaded = 0
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            stream.write(chunk)
            if progress and total:
                downloaded += len(chunk)
                progress(downloaded, total)
    actual = digest.hexdigest()
    if actual != expected:
        destination.unlink(missing_ok=True)
        raise InstallError(f"Download checksum mismatch: {url}")
    return actual


def _download_installer(url: str, destination: Path) -> str:
    expected_text = _get_bytes(url + ".sha1", 200).decode("ascii", "replace")
    match = re.search(r"\b[0-9a-fA-F]{40}\b", expected_text)
    if not match:
        raise InstallError(f"No valid SHA-1 checksum for {url}")
    return _download_checked(url, destination, match.group(0))


def _latest_fabric_loader(minecraft_version: str) -> str:
    versions = _get_json(f"{FABRIC_META}/loader/{quote(minecraft_version)}")
    if not isinstance(versions, list):
        raise InstallError("Fabric returned an invalid loader list")
    loaders = [item.get("loader") for item in versions if isinstance(item, dict)]
    stable = next((item for item in loaders if isinstance(item, dict)
                   and item.get("stable") and isinstance(item.get("version"), str)), None)
    selected = stable or next((item for item in loaders if isinstance(item, dict)
                               and isinstance(item.get("version"), str)), None)
    if not selected:
        raise InstallError(f"No Fabric loader for Minecraft {minecraft_version}")
    return selected["version"]


def _latest_quilt_loader(minecraft_version: str) -> str:
    supported = _get_json(f"{QUILT_META}/loader/{quote(minecraft_version)}")
    ordered = _get_json(f"{QUILT_META}/loader")
    if not isinstance(supported, list) or not isinstance(ordered, list):
        raise InstallError("Quilt returned an invalid loader list")
    compatible = {item["loader"]["version"] for item in supported
                  if isinstance(item, dict) and isinstance(item.get("loader"), dict)
                  and isinstance(item["loader"].get("version"), str)}
    for item in ordered:
        if (isinstance(item, dict) and isinstance(item.get("version"), str)
                and item["version"] in compatible and "-" not in item["version"]):
            return item["version"]
    raise InstallError(f"No stable Quilt loader for Minecraft {minecraft_version}")


def _latest_forge_loader(minecraft_version: str) -> str:
    promotions = _get_json(FORGE_PROMOTIONS)
    promos = promotions.get("promos") if isinstance(promotions, dict) else None
    if not isinstance(promos, dict):
        raise InstallError("Forge returned an invalid promotions list")
    version = promos.get(f"{minecraft_version}-recommended") or promos.get(
        f"{minecraft_version}-latest"
    )
    if not isinstance(version, str):
        raise InstallError(f"No Forge release for Minecraft {minecraft_version}")
    return version


def _neoforge_versions(minecraft_version: str) -> list[str]:
    legacy = minecraft_version == "1.20.1"
    try:
        metadata = ET.fromstring(_get_bytes(
            NEOFORGE_LEGACY_METADATA if legacy else NEOFORGE_METADATA
        ))
    except ET.ParseError as error:
        raise InstallError("NeoForge returned invalid Maven metadata") from error
    parts = minecraft_version.split(".")
    prefix = "1.20.1-" if legacy else ".".join(parts[1:] if parts[0] == "1" else parts) + "."
    return [element.text for element in metadata.findall("./versioning/versions/version")
            if element.text and element.text.startswith(prefix)
            and (legacy or re.fullmatch(r"\d+(?:-[\w.+-]+)?", element.text[len(prefix):]))]


def _latest_neoforge_loader(minecraft_version: str) -> str:
    stable = [version for version in _neoforge_versions(minecraft_version)
              if minecraft_version == "1.20.1" or "-" not in version]
    if not stable:
        raise InstallError(f"No stable NeoForge release for Minecraft {minecraft_version}; "
                           "specify --loader-version")
    return max(stable, key=lambda version: tuple(
        int(part) for part in re.findall(r"\d+", version)
    ))


def _resolve_loader_version(spec: ServerSpec) -> str:
    if spec.loader_version:
        return spec.loader_version
    resolvers = {
        "fabric": _latest_fabric_loader,
        "quilt": _latest_quilt_loader,
        "forge": _latest_forge_loader,
        "neoforge": _latest_neoforge_loader,
    }
    return resolvers[spec.loader](spec.minecraft_version)


def _installer_url(loader: str, minecraft: str, version: str) -> tuple[str, str]:
    if loader == "forge":
        artifact = version if version.startswith(f"{minecraft}-") else f"{minecraft}-{version}"
        url = ("https://maven.minecraftforge.net/net/minecraftforge/forge/"
               f"{artifact}/forge-{artifact}-installer.jar")
        return url, version
    if loader == "neoforge":
        if minecraft == "1.20.1":
            artifact = (version if version.startswith("1.20.1-")
                        else f"1.20.1-{version}")
            url = ("https://maven.neoforged.net/releases/net/neoforged/forge/"
                   f"{artifact}/forge-{artifact}-installer.jar")
            return url, version
        url = ("https://maven.neoforged.net/releases/net/neoforged/neoforge/"
               f"{version}/neoforge-{version}-installer.jar")
        return url, version
    meta_url = f"{FABRIC_META}/installer" if loader == "fabric" else f"{QUILT_META}/installer"
    installers = _get_json(meta_url)
    if not isinstance(installers, list):
        raise InstallError(f"{loader} returned an invalid installer list")
    if loader == "fabric":
        selected = next((item for item in installers if isinstance(item, dict)
                         and item.get("stable") and isinstance(item.get("url"), str)), None)
    else:
        selected = None
    selected = selected or next((item for item in installers if isinstance(item, dict)
                                 and isinstance(item.get("url"), str)), None)
    if not selected:
        raise InstallError(f"No {loader} installer found")
    return selected["url"], selected["version"]


def _manifest_line(name: str, value: str) -> bytes:
    line = f"{name}: {value}".encode("ascii")
    parts = [line[:70]]
    line = line[70:]
    while line:
        parts.append(b" " + line[:69])
        line = line[69:]
    return b"\r\n".join(parts) + b"\r\n"


def _install_quilt(minecraft: str, version: str, directory: Path,
                   minecraft_details: dict) -> str:
    """Install Quilt from its official server profile and Mojang's server JAR."""
    profile_url = f"{QUILT_META}/loader/{quote(minecraft)}/{quote(version)}/server/json"
    profile = _get_json(profile_url)
    libraries = profile.get("libraries") if isinstance(profile, dict) else None
    main_class = profile.get("launcherMainClass") if isinstance(profile, dict) else None
    if (not isinstance(libraries, list) or not libraries
            or not isinstance(main_class, str)
            or not re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+",
                                main_class)):
        raise InstallError("Quilt returned an invalid server library list")
    library_paths: list[Path] = []
    for library in libraries:
        if not isinstance(library, dict):
            raise InstallError("Quilt returned an invalid library entry")
        coordinate = library.get("name")
        base = library.get("url")
        if not isinstance(coordinate, str) or not isinstance(base, str):
            raise InstallError("Quilt returned an invalid library entry")
        parts = coordinate.split(":")
        if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_.+\-]+", part)
                                         for part in parts):
            raise InstallError(f"Invalid Quilt library coordinate: {coordinate}")
        if base not in {"https://maven.fabricmc.net/",
                        "https://maven.quiltmc.org/repository/release/"}:
            raise InstallError(f"Unexpected Quilt library repository: {base}")
        group, artifact, library_version = parts
        relative = Path(group.replace(".", "/")) / artifact / library_version / (
            f"{artifact}-{library_version}.jar"
        )
        destination = directory / "libraries" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _download_installer(base + relative.as_posix(), destination)
        library_paths.append(destination.relative_to(directory))

    manifest = (b"Manifest-Version: 1.0\r\n"
                + _manifest_line("Main-Class", main_class)
                + _manifest_line("Class-Path", " ".join(
                    path.as_posix() for path in library_paths
                )) + b"\r\n")
    with zipfile.ZipFile(directory / "quilt-server-launch.jar", "w") as launcher:
        launcher.writestr("META-INF/MANIFEST.MF", manifest)

    downloads = minecraft_details.get("downloads")
    server = downloads.get("server") if isinstance(downloads, dict) else None
    if (not isinstance(server, dict) or not isinstance(server.get("url"), str)
            or not isinstance(server.get("sha1"), str)):
        raise InstallError("Mojang did not provide a server download for this version")
    _download_checked(server["url"], directory / "server.jar", server["sha1"])
    return profile_url


def _jdk_cache_dir() -> Path:
    override = os.environ.get("SRVCRT_JDK_DIR") or os.environ.get("SRVCRT_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "srvcrt" / "jdks"


def _host_jdk_platform() -> tuple[str, str, str, str, str]:
    if sys.platform.startswith("linux"):
        adoptium_os, azul_os, ext = "linux", "linux", "tar.gz"
    elif sys.platform == "darwin":
        adoptium_os, azul_os, ext = "mac", "macos", "tar.gz"
    elif os.name == "nt" or sys.platform in {"win32", "cygwin", "msys"}:
        adoptium_os, azul_os, ext = "windows", "windows", "zip"
    else:
        raise InstallError(f"Unsupported operating system for portable Java: {sys.platform}")

    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        adoptium_arch, azul_arch = "x64", "x86"
    elif machine in {"aarch64", "arm64"}:
        adoptium_arch, azul_arch = "aarch64", "arm"
    else:
        raise InstallError(f"Unsupported CPU architecture for portable Java: {machine}")
    return adoptium_os, adoptium_arch, azul_os, azul_arch, ext


def _portable_jdk_download(major: int) -> tuple[str, str, str]:
    adoptium_os, adoptium_arch, azul_os, azul_arch, ext = _host_jdk_platform()
    candidates = (major, 17) if major == 16 else (major,)
    last_error: InstallError | None = None

    for version in candidates:
        adoptium_url = (
            f"{ADOPTIUM_API}/{version}/hotspot"
            f"?architecture={adoptium_arch}&image_type=jdk&os={adoptium_os}&vendor=eclipse"
        )
        try:
            assets = _get_json(adoptium_url)
            if isinstance(assets, list):
                for item in assets:
                    pkg = item.get("binary", {}).get("package", {}) if isinstance(item, dict) else {}
                    link = pkg.get("link")
                    checksum = pkg.get("checksum")
                    if (isinstance(link, str) and link.startswith("https://")
                            and isinstance(checksum, str)
                            and re.fullmatch(r"[0-9a-fA-F]{64}", checksum)):
                        return link, checksum.lower(), ext
        except InstallError as error:
            last_error = error

        azul_url = (
            f"{AZUL_API}?java_version={version}&os={azul_os}&arch={azul_arch}"
            f"&hw_bitness=64&archive_type={ext}&java_package_type=jdk"
            "&javafx_bundled=false&latest=true&release_status=ga"
            "&availability_types=CA&page=1&page_size=1&include_fields=sha256_hash"
        )
        try:
            packages = _get_json(azul_url)
            if isinstance(packages, list):
                for item in packages:
                    if not isinstance(item, dict):
                        continue
                    link = item.get("download_url")
                    checksum = item.get("sha256_hash")
                    if (isinstance(link, str) and link.startswith("https://")
                            and isinstance(checksum, str)
                            and re.fullmatch(r"[0-9a-fA-F]{64}", checksum)):
                        return link, checksum.lower(), ext
        except InstallError as error:
            last_error = error

    detail = f": {last_error}" if last_error else ""
    raise InstallError(
        f"No portable Java {major} build found for {adoptium_os}/{adoptium_arch}{detail}"
    )


def _find_java_binary(jdk_dir: Path) -> Path | None:
    if not jdk_dir.is_dir():
        return None
    names = ("java.exe", "java") if os.name == "nt" else ("java",)
    for name in names:
        for relative in (
            Path("bin") / name,
            Path("Contents") / "Home" / "bin" / name,
        ):
            candidate = jdk_dir / relative
            if candidate.is_file():
                return candidate
        for pattern in (f"*/bin/{name}", f"*/Contents/Home/bin/{name}"):
            for candidate in sorted(jdk_dir.glob(pattern)):
                if candidate.is_file():
                    return candidate
    return None


def _safe_extract_archive(archive: Path, destination: Path, ext: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if ext == "zip":
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                target = (root / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise InstallError(f"Unsafe path in JDK archive: {member.filename}")
            zf.extractall(root)
        if os.name != "nt":
            for path in root.rglob("*"):
                if path.is_file() and (
                    path.parent.name == "bin" or path.name == "jspawnhelper"
                ):
                    path.chmod(0o755)
        return

    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            parts = Path(member.name).parts
            if Path(member.name).is_absolute() or ".." in parts:
                raise InstallError(f"Unsafe path in JDK archive: {member.name}")
            if member.issym() or member.islnk():
                link_target = (root / Path(member.name).parent / member.linkname).resolve()
                if link_target != root and root not in link_target.parents:
                    raise InstallError(f"Unsafe link in JDK archive: {member.name}")
        if "filter" in inspect.signature(tf.extractall).parameters:
            tf.extractall(root, filter="tar")
        else:
            tf.extractall(root)


def _hardlink_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _link_or_copy_jdk(source_jdk: Path, target_jdk: Path) -> None:
    if target_jdk.exists() or target_jdk.is_symlink():
        if target_jdk.is_symlink() or target_jdk.is_file():
            target_jdk.unlink()
        else:
            shutil.rmtree(target_jdk)
    shutil.copytree(source_jdk, target_jdk, symlinks=True, copy_function=_hardlink_or_copy)


def install_portable_jdk(
    required_java: int,
    directory: Path | None = None,
    *,
    cache_dir: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download and cache a portable OpenJDK for required_java, optionally linking it into directory/jdk."""
    adoptium_os, adoptium_arch, _, _, _ = _host_jdk_platform()
    cache_root = (cache_dir or _jdk_cache_dir()).expanduser().resolve()
    cached = cache_root / f"jdk-{required_java}-{adoptium_os}-{adoptium_arch}"

    java_bin = _find_java_binary(cached)
    if java_bin is not None:
        try:
            if _java_major(str(java_bin)) < required_java:
                java_bin = None
        except InstallError:
            java_bin = None

    if java_bin is None:
        url, sha256, ext = _portable_jdk_download(required_java)
        cache_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".jdk-{required_java}.tmp-", dir=cache_root))
        try:
            archive = staging / f"jdk.{ext}"
            _download_checked(url, archive, sha256, "sha256", progress=progress)
            extracted = staging / "extracted"
            _safe_extract_archive(archive, extracted, ext)
            entries = [item for item in extracted.iterdir() if not item.name.startswith(".")]
            source = entries[0] if len(entries) == 1 and entries[0].is_dir() else extracted
            if _find_java_binary(source) is None:
                raise InstallError(f"Portable Java {required_java} archive did not contain bin/java")
            if cached.exists():
                shutil.rmtree(cached)
            source.rename(cached)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        java_bin = _find_java_binary(cached)
        if java_bin is None:
            raise InstallError(f"Portable Java {required_java} installation is incomplete")
        _java_major(str(java_bin))

    if directory is not None:
        local_jdk = directory / "jdk"
        _link_or_copy_jdk(cached, local_jdk)
        local_bin = _find_java_binary(local_jdk)
        if local_bin is None:
            raise InstallError(f"Failed to copy portable Java {required_java} into server directory")
        return local_bin
    return java_bin


def ensure_java(
    java: str,
    required_java: int,
    directory: Path | None = None,
    *,
    cache_dir: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """Return a Java executable matching required_java, installing a portable JDK if needed."""
    try:
        installed_java: int | None = _java_major(java)
    except InstallError as error:
        if java != "java":
            raise
        installed_java = None

    if java != "java":
        if installed_java is not None and installed_java < required_java:
            raise InstallError(
                f"Java {required_java}+ is required; found Java {installed_java} on this computer"
            )
        return java

    if directory is None and installed_java == required_java:
        return java

    try:
        portable_bin = install_portable_jdk(
            required_java, directory, cache_dir=cache_dir, progress=progress,
        )
        return str(portable_bin)
    except InstallError:
        if installed_java is not None and installed_java >= required_java:
            return java
        raise


def _run_installer(command: list[str], directory: Path) -> None:
    log = directory / "srvcrt-installer.log"
    try:
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=directory, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError(f"Server installer failed: {error}") from error
    if result.returncode:
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
        raise InstallError(f"Server installer exited with code {result.returncode}:\n{tail}")


def _write_start_script(loader: str, directory: Path) -> None:
    if loader in {"forge", "neoforge"}:
        if not (directory / "run.sh").is_file():
            raise InstallError("Installer did not create run.sh")
        (directory / "run.sh").chmod(0o755)
        jvm_args = directory / "user_jvm_args.txt"
        if jvm_args.is_file():
            contents = jvm_args.read_text(encoding="utf-8")
            if not re.search(r"(?m)^\s*-Xmx", contents):
                with jvm_args.open("a", encoding="utf-8") as stream:
                    stream.write("\n-Xms1G\n-Xmx4G\n")
        command = 'exec ./run.sh nogui "$@"'
    else:
        launcher = f"{loader}-server-launch.jar" if loader in {"fabric", "quilt"} else "server.jar"
        if not (directory / launcher).is_file() or not (directory / "server.jar").is_file():
            raise InstallError(f"Installer did not create the required server JARs for {loader}")
        nogui = "--nogui" if loader in {"paper", "purpur"} else "nogui"
        command = (f'exec "${{JAVA_BIN:-java}}" "-Xms${{JAVA_XMS:-1G}}" '
                   f'"-Xmx${{JAVA_XMX:-4G}}" -jar {launcher} {nogui} "$@"')
    script = directory / "start.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cd "$(dirname "$0")"\n'
        'for candidate in "./jdk/bin/java" "./jdk/Contents/Home/bin/java"; do\n'
        '    if [ -x "$candidate" ] && "$candidate" -version >/dev/null 2>&1; then\n'
        '        export JAVA_HOME="$(cd "$(dirname "$candidate")/.." && pwd)"\n'
        '        export PATH="$JAVA_HOME/bin:$PATH"\n'
        '        : "${JAVA_BIN:=$JAVA_HOME/bin/java}"\n'
        "        break\n"
        "    fi\n"
        "done\n"
        f"{command}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def install_runtime(spec: ServerSpec, directory: Path, java: str = "java", *,
                    minecraft_details: dict | None = None,
                    progress: Callable[[int], None] | None = None) -> dict:
    """Create the complete server runtime inside the staging directory."""
    if minecraft_details is None:
        minecraft_details = _minecraft_details(spec.minecraft_version)
    required_java = _minecraft_java_major(spec.minecraft_version, minecraft_details)
    java = ensure_java(
        java,
        required_java,
        directory,
        progress=(
            (lambda done, total: progress(min(14, done * 14 // total)))
            if progress else None
        ),
    )
    version = _resolve_loader_version(spec)
    if progress:
        progress(15)
    if spec.loader == "quilt":
        profile_url = _install_quilt(
            spec.minecraft_version, version, directory, minecraft_details,
        )
        if progress:
            progress(85)
        _write_start_script(spec.loader, directory)
        return {
            "minecraft_version": spec.minecraft_version,
            "loader": spec.loader,
            "loader_version": version,
            "installer_version": None,
            "installer_url": profile_url,
            "installer_sha1": None,
            "required_java": required_java,
            "start_script": "start.sh",
        }
    url, installer_version = _installer_url(spec.loader, spec.minecraft_version, version)
    installer = directory / f"{spec.loader}-installer.jar"
    installer_sha1 = _download_installer(url, installer)
    if progress:
        progress(35)
    if spec.loader == "fabric":
        command = [java, "-jar", str(installer), "server", "-dir", str(directory),
                   "-mcversion", spec.minecraft_version, "-loader", version,
                   "-downloadMinecraft"]
    else:
        command = [java, "-jar", str(installer), "--installServer"]
    _run_installer(command, directory)
    if progress:
        progress(85)
    installer.unlink()
    if spec.loader in {"forge", "neoforge"} and not (directory / "libraries").is_dir():
        raise InstallError("Installer did not create the server libraries")
    _write_start_script(spec.loader, directory)
    return {
        "minecraft_version": spec.minecraft_version,
        "loader": spec.loader,
        "loader_version": version,
        "installer_version": installer_version,
        "installer_url": url,
        "installer_sha1": installer_sha1,
        "required_java": required_java,
        "start_script": "start.sh",
    }

