"""Copy server configuration while leaving identifiable client settings out."""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

from .jsonc import loads as load_mod_json


SHARED_DIRECTORIES = (
    "config",
    "defaultconfigs",
    "kubejs",
    "scripts",
    "datapacks",
    "global_packs",
    "openloader",
    "patchouli_books",
)
CLIENT_CONFIG_SUFFIX = re.compile(r"(?:[-_.]client)(?:[-_.]|$)", re.IGNORECASE)
MOD_ID = re.compile(r'\bmodId\s*=\s*["\']([A-Za-z0-9_.-]+)["\']')


def mod_ids(path: Path) -> set[str]:
    """Read declared IDs from Fabric, Quilt, Forge, and NeoForge metadata."""
    ids: set[str] = set()
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names:
                with jar.open("fabric.mod.json") as stream:
                    data = load_mod_json(stream.read().decode("utf-8"))
                if isinstance(data, dict) and isinstance(data.get("id"), str):
                    ids.add(data["id"].lower())
                    provides = data.get("provides")
                    if isinstance(provides, list):
                        ids.update(item.lower() for item in provides if isinstance(item, str))
            if "quilt.mod.json" in names:
                with jar.open("quilt.mod.json") as stream:
                    data = load_mod_json(stream.read().decode("utf-8"))
                quilt_loader = data.get("quilt_loader") if isinstance(data, dict) else None
                if isinstance(quilt_loader, dict) and isinstance(quilt_loader.get("id"), str):
                    ids.add(quilt_loader["id"].lower())
            for filename in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if filename in names:
                    with jar.open(filename) as stream:
                        source = stream.read(1_000_000).decode("utf-8", "replace")
                    ids.update(value.lower() for value in MOD_ID.findall(source))
    except (OSError, zipfile.BadZipFile, ValueError, UnicodeError, KeyError):
        return ids
    return ids


def _owner(relative: Path, sides: dict[str, str]) -> tuple[str, str] | None:
    first = relative.parts[0].lower()
    candidates = [first]
    if len(relative.parts) > 1:
        candidates.append(relative.name.lower())
    for name in candidates:
        for mod_id in sorted(sides, key=len, reverse=True):
            if name == mod_id or any(name.startswith(mod_id + separator)
                                     for separator in ("-", "_", ".")):
                return mod_id, sides[mod_id]
    return None


def copy_server_files(
    game: Path,
    destination: Path,
    mods: list[tuple[Path, str]],
    warnings: list[str],
) -> list[dict]:
    sides: dict[str, str] = {}
    for path, side in mods:
        for mod_id in mod_ids(path):
            old = sides.get(mod_id)
            sides[mod_id] = side if old is None or old == side else "review"

    decisions: list[dict] = []
    for directory_name in SHARED_DIRECTORIES:
        source = game / directory_name
        if not source.is_dir() or source.is_symlink():
            continue
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                warnings.append(f"Skipped symlink: {path}")
                continue
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            action = "copy"
            reason = "shared server files"
            if directory_name in {"config", "defaultconfigs"}:
                found = _owner(relative, sides)
                if found and found[1] == "client":
                    action, reason = "skip", f"client-only mod: {found[0]}"
                elif found and found[1] == "review":
                    action, reason = "review", f"unresolved mod: {found[0]}"
                elif CLIENT_CONFIG_SUFFIX.search(relative.name):
                    action, reason = "skip", "client-specific config filename"
                elif found:
                    reason = f"server mod: {found[0]}"
                else:
                    reason = "unattributed config; kept for compatibility"
            elif directory_name == "kubejs" and relative.parts[0] in {
                "client_scripts", "assets"
            }:
                action, reason = "skip", "client-side KubeJS files"

            if action == "copy":
                target = destination / directory_name / relative
            elif action == "review":
                target = destination / "review" / directory_name / relative
            else:
                target = None
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            decisions.append({"path": str(Path(directory_name) / relative),
                              "action": action, "reason": reason})

    properties = game / "server.properties"
    if properties.is_file() and not properties.is_symlink():
        shutil.copy2(properties, destination / "server.properties")
        decisions.append({"path": "server.properties", "action": "copy",
                          "reason": "existing server settings"})
    return decisions
