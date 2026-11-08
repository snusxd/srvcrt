"""Detect the Minecraft and mod-loader versions of an installed instance."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


LOADERS = {"fabric", "forge", "neoforge", "quilt"}
UID_TO_LOADER = {
    "net.fabricmc.fabric-loader": "fabric",
    "net.minecraftforge": "forge",
    "net.neoforged": "neoforge",
    "org.quiltmc.quilt-loader": "quilt",
}
VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]*\Z")
MINECRAFT_VERSION = re.compile(r"(?:1\.)?\d+(?:\.\d+){1,2}\Z")


@dataclass(frozen=True)
class ServerSpec:
    minecraft_version: str
    loader: str
    loader_version: str | None
    detected_from: str


@dataclass(frozen=True)
class _Hint:
    minecraft_version: str | None = None
    loader: str | None = None
    loader_version: str | None = None
    source: str = ""


def _read_json(path: Path) -> dict | None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def instance_root(path: Path) -> Path:
    if path.name in {"minecraft", ".minecraft"} and (path.parent / "mmc-pack.json").is_file():
        return path.parent
    return path


def _metadata_files(client: Path, game: Path, filename: str) -> list[Path]:
    roots = [client, game, instance_root(game)]
    if client.name == ".minecraft" or game.name == ".minecraft":
        roots.append(game.parent)
    return list(dict.fromkeys(root / filename for root in roots))


def _from_mmc_pack(data: dict, source: str) -> _Hint | None:
    components = data.get("components")
    if not isinstance(components, list):
        return None
    minecraft = None
    loader = None
    loader_version = None
    for component in components:
        if not isinstance(component, dict):
            continue
        uid = component.get("uid")
        version = component.get("version")
        if not isinstance(version, str):
            continue
        if uid == "net.minecraft":
            minecraft = version
        elif uid in UID_TO_LOADER:
            loader = UID_TO_LOADER[uid]
            loader_version = version
    if minecraft or loader:
        return _Hint(minecraft, loader, loader_version, source)
    return None


def _from_curseforge_manifest(data: dict, source: str) -> _Hint | None:
    minecraft = data.get("minecraft")
    if not isinstance(minecraft, dict):
        return None
    version = minecraft.get("version")
    mod_loaders = minecraft.get("modLoaders", [])
    if not isinstance(mod_loaders, list):
        mod_loaders = []
    primary = next((item for item in mod_loaders
                    if isinstance(item, dict) and item.get("primary")), None)
    if primary is None:
        primary = next((item for item in mod_loaders if isinstance(item, dict)), None)
    loader = loader_version = None
    if primary and isinstance(primary.get("id"), str):
        match = re.fullmatch(r"(forge|neoforge|fabric|quilt)-(.+)",
                             primary["id"], re.IGNORECASE)
        if match:
            loader, loader_version = match.group(1).lower(), match.group(2)
    if isinstance(version, str) or loader:
        return _Hint(version if isinstance(version, str) else None,
                     loader, loader_version, source)
    return None


def _from_profile_json(data: dict, source: str) -> _Hint | None:
    minecraft = data.get("game_version") or data.get("minecraft_version")
    loader = data.get("loader")
    loader_version = data.get("loader_version")
    if isinstance(minecraft, str) or (isinstance(loader, str) and loader in LOADERS):
        return _Hint(minecraft if isinstance(minecraft, str) else None,
                     loader if isinstance(loader, str) and loader in LOADERS else None,
                     loader_version if isinstance(loader_version, str) else None,
                     source)
    return None


def _from_version_id(identifier: str, source: str,
                     inherits_from: str | None = None) -> _Hint | None:
    patterns = (
        (r"fabric-loader-(.+?)-((?:1\.)?\d+(?:\.\d+){1,2})", "fabric", 2, 1),
        (r"quilt-loader-(.+?)-((?:1\.)?\d+(?:\.\d+){1,2})", "quilt", 2, 1),
        (r"((?:1\.)?\d+(?:\.\d+){1,2})-forge-(.+)", "forge", 1, 2),
        (r"((?:1\.)?\d+(?:\.\d+){1,2})-neoforge-(.+)", "neoforge", 1, 2),
        (r"neoforge-(.+)", "neoforge", 0, 1),
    )
    for pattern, loader, mc_group, loader_group in patterns:
        match = re.fullmatch(pattern, identifier, re.IGNORECASE)
        if match:
            minecraft = match.group(mc_group) if mc_group else inherits_from
            return _Hint(minecraft, loader, match.group(loader_group), source)
    if MINECRAFT_VERSION.fullmatch(identifier):
        return _Hint(identifier, source=source)
    return None


def _from_launcher_profiles(data: dict, source: str) -> _Hint | None:
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return None
    selected = data.get("selectedProfile")
    profile = profiles.get(selected) if isinstance(selected, str) else None
    if profile is None and len(profiles) == 1:
        profile = next(iter(profiles.values()))
    if not isinstance(profile, dict):
        return None
    identifier = profile.get("lastVersionId")
    if isinstance(identifier, str):
        return _from_version_id(identifier, source)
    return None


def _from_versions_directory(game: Path) -> _Hint | None:
    versions_dir = game / "versions"
    if not versions_dir.is_dir():
        return None
    candidates: list[_Hint] = []
    for path in versions_dir.glob("*/*.json"):
        data = _read_json(path)
        if not data:
            continue
        identifier = data.get("id", path.stem)
        inherits = data.get("inheritsFrom")
        if not isinstance(identifier, str):
            continue
        hint = _from_version_id(identifier, str(path),
                                inherits if isinstance(inherits, str) else None)
        if hint and hint.loader:
            candidates.append(hint)
    return candidates[0] if len(candidates) == 1 else None


def _from_log(game: Path) -> _Hint | None:
    path = game / "logs" / "latest.log"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            text = stream.read(2_000_000)
    except OSError:
        return None
    match = re.search(r"Loading Minecraft (\S+) with (Fabric|Quilt) Loader (\S+)", text)
    if match:
        return _Hint(match.group(1), match.group(2).lower(), match.group(3), str(path))
    mc = re.search(r"--fml\.mcVersion(?:,|\s)+([A-Za-z0-9.]+)", text)
    neo = re.search(r"--fml\.neoForgeVersion(?:,|\s)+([A-Za-z0-9.\-]+)", text)
    forge = re.search(r"--fml\.forgeVersion(?:,|\s)+([A-Za-z0-9.\-]+)", text)
    if mc and (neo or forge):
        return _Hint(mc.group(1), "neoforge" if neo else "forge",
                     (neo or forge).group(1), str(path))
    return None


def _from_modrinth(versions: list[dict]) -> _Hint | None:
    minecraft_sets = []
    loader_sets = []
    for version in versions:
        games = version.get("game_versions")
        loaders = version.get("loaders")
        if isinstance(games, list):
            values = {game for game in games
                      if isinstance(game, str) and MINECRAFT_VERSION.fullmatch(game)}
            if values:
                minecraft_sets.append(values)
        if isinstance(loaders, list):
            values = {loader for loader in loaders
                      if isinstance(loader, str) and loader in LOADERS}
            if values:
                loader_sets.append(values)
    games = set.intersection(*minecraft_sets) if minecraft_sets else set()
    loaders = set.intersection(*loader_sets) if loader_sets else set()
    minecraft = next(iter(games)) if len(games) == 1 else None
    loader = next(iter(loaders)) if len(loaders) == 1 else None
    return _Hint(minecraft, loader, source="Modrinth version metadata") if minecraft or loader else None


def _from_jar_metadata(paths: list[Path]) -> _Hint | None:
    loaders: set[str] = set()
    for path in paths:
        try:
            with zipfile.ZipFile(path) as jar:
                names = set(jar.namelist())
        except (OSError, zipfile.BadZipFile):
            continue
        if "META-INF/neoforge.mods.toml" in names:
            loaders.add("neoforge")
        elif "META-INF/mods.toml" in names:
            loaders.add("forge")
        elif "quilt.mod.json" in names:
            loaders.add("quilt")
        elif "fabric.mod.json" in names:
            loaders.add("fabric")
    if "quilt" in loaders and "fabric" in loaders:
        loaders.remove("fabric")  # Quilt also accepts many Fabric mods.
    return _Hint(loader=next(iter(loaders)), source="JAR metadata") if len(loaders) == 1 else None


def detect_server_spec(
    client: Path,
    game: Path,
    mod_paths: list[Path],
    modrinth_versions: list[dict],
    *,
    minecraft_version: str | None = None,
    loader: str | None = None,
    loader_version: str | None = None,
) -> ServerSpec:
    hints: list[_Hint] = []
    for path in _metadata_files(client, game, "mmc-pack.json"):
        data = _read_json(path)
        if data and (hint := _from_mmc_pack(data, str(path))):
            hints.append(hint)
    for path in _metadata_files(client, game, "manifest.json"):
        data = _read_json(path)
        if data and (hint := _from_curseforge_manifest(data, str(path))):
            hints.append(hint)
    for path in _metadata_files(client, game, "profile.json"):
        data = _read_json(path)
        if data and (hint := _from_profile_json(data, str(path))):
            hints.append(hint)
    for path in _metadata_files(client, game, "launcher_profiles.json"):
        data = _read_json(path)
        if data and (hint := _from_launcher_profiles(data, str(path))):
            hints.append(hint)
    for hint in (_from_versions_directory(game), _from_log(game),
                 _from_modrinth(modrinth_versions), _from_jar_metadata(mod_paths)):
        if hint:
            hints.append(hint)

    paired = next((hint for hint in hints
                   if hint.minecraft_version and hint.loader
                   and (minecraft_version is None
                        or hint.minecraft_version == minecraft_version)
                   and (loader is None or hint.loader == loader)), None)
    minecraft_values = {hint.minecraft_version for hint in hints if hint.minecraft_version}
    loader_values = {hint.loader for hint in hints if hint.loader}
    if minecraft_version is None and paired is None and len(minecraft_values) > 1:
        raise ValueError("Conflicting Minecraft versions detected; specify "
                         "--minecraft-version")
    if loader is None and paired is None and len(loader_values) > 1:
        raise ValueError("Conflicting mod loaders detected; specify --loader")
    mc = (minecraft_version or (paired.minecraft_version if paired else None)
          or next(iter(minecraft_values), None))
    selected_loader = (loader or (paired.loader if paired else None)
                       or next(iter(loader_values), None))
    detected_loader_version = next((hint.loader_version for hint in hints
                                    if hint.loader == selected_loader
                                    and hint.minecraft_version == mc
                                    and hint.loader_version), None)
    selected_version = loader_version or detected_loader_version
    if not mc or not selected_loader:
        missing = []
        if not mc:
            missing.append("--minecraft-version")
        if not selected_loader:
            missing.append("--loader")
        raise ValueError("Cannot determine server core; specify " + " and ".join(missing))
    if not MINECRAFT_VERSION.fullmatch(mc):
        raise ValueError(f"Invalid Minecraft version: {mc}")
    if selected_loader not in LOADERS:
        raise ValueError(f"Unsupported loader: {selected_loader}")
    if selected_version and not VERSION_TOKEN.fullmatch(selected_version):
        raise ValueError(f"Invalid loader version: {selected_version}")
    source = ("CLI options" if minecraft_version or loader or loader_version else
              paired.source if paired else
              next((hint.source for hint in hints if hint.minecraft_version == mc
                    or hint.loader == selected_loader), "unknown"))
    return ServerSpec(mc, selected_loader, selected_version, source)
