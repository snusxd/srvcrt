"""Conservative classification of a mod's dedicated-server compatibility."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .jsonc import loads as load_mod_json


SERVER_ENVIRONMENTS = {
    "client_and_server",
    "client_only_server_optional",
    "server_only",
    "server_only_client_optional",
    "dedicated_server_only",
    "client_or_server",
    "client_or_server_prefers_both",
}
CLIENT_ENVIRONMENTS = {"client_only", "singleplayer_only"}


def mod_display_name(path: Path) -> str:
    """Return the name declared by a mod, falling back to its JAR filename."""
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names:
                with jar.open("fabric.mod.json") as stream:
                    data = load_mod_json(stream.read().decode("utf-8"))
                if isinstance(data, dict):
                    value = data.get("name") or data.get("id")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            if "quilt.mod.json" in names:
                with jar.open("quilt.mod.json") as stream:
                    data = load_mod_json(stream.read().decode("utf-8"))
                loader = data.get("quilt_loader") if isinstance(data, dict) else None
                if isinstance(loader, dict):
                    metadata = loader.get("metadata")
                    value = (metadata.get("name") if isinstance(metadata, dict) else None)
                    value = value or loader.get("id")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            for filename in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if filename in names:
                    with jar.open(filename) as stream:
                        source = stream.read(1_000_000).decode("utf-8", "replace")
                    match = re.search(r'''(?m)^\s*displayName\s*=\s*["']([^"']+)["']''',
                                      source)
                    if match and not match.group(1).startswith("${"):
                        return match.group(1).strip()
    except (OSError, zipfile.BadZipFile, ValueError, UnicodeError):
        pass
    return path.stem


def modrinth_side(version: dict | None) -> tuple[str, str] | None:
    if not isinstance(version, dict):
        return None
    environment = version.get("environment")
    if environment in SERVER_ENVIRONMENTS:
        return "server", f"Modrinth environment: {environment}"
    if environment in CLIENT_ENVIRONMENTS:
        return "client", f"Modrinth environment: {environment}"
    # Older Modrinth versions expose only the two legacy side fields.
    server_side = version.get("server_side")
    if server_side in {"required", "optional"}:
        return "server", f"Modrinth server_side: {server_side}"
    if server_side == "unsupported":
        return "client", "Modrinth server_side: unsupported"
    return None


def local_side(path: Path) -> tuple[str, str] | None:
    """Read explicit mod metadata declarations, if present.

    Forge's clientSideOnly=true is unambiguous. An absent or false value does
    not prove dedicated-server compatibility, so it still requires review.
    """
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names:
                with jar.open("fabric.mod.json") as stream:
                    metadata = load_mod_json(stream.read().decode("utf-8"))
                if not isinstance(metadata, dict):
                    return None
                environment = metadata.get("environment", "*")
                if environment == "client":
                    return "client", "fabric.mod.json environment: client"
                if environment in {"server", "*"}:
                    return "server", f"fabric.mod.json environment: {environment}"
            if "quilt.mod.json" in names:
                with jar.open("quilt.mod.json") as stream:
                    metadata = load_mod_json(stream.read().decode("utf-8"))
                quilt_loader = metadata.get("quilt_loader") if isinstance(metadata, dict) else None
                environment = (quilt_loader.get("environment")
                               if isinstance(quilt_loader, dict) else None)
                if environment == "client":
                    return "client", "quilt.mod.json environment: client"
                if environment in {"server", "*"}:
                    return "server", f"quilt.mod.json environment: {environment}"
            for filename in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if filename not in names:
                    continue
                with jar.open(filename) as stream:
                    # The declaration is a top-level TOML key. Only an
                    # explicit true value is safe to classify automatically.
                    for line in stream.read().decode("utf-8").splitlines():
                        line = line.split("#", 1)[0].strip()
                        if line.startswith("["):
                            break
                        if re.fullmatch(r"clientSideOnly\s*=\s*true", line):
                            return "client", f"{Path(filename).name} clientSideOnly: true"
    except (OSError, zipfile.BadZipFile, ValueError, UnicodeError):
        return None
    return None
