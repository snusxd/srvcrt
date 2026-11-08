"""Create a server-ready mod directory and an audit report."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .api import APIError, BATCH_SIZE, ModAPI
from .configs import copy_server_files
from .fingerprint import curseforge_fingerprint, sha1_file
from .instance import ServerSpec, detect_server_spec, instance_root
from .metadata import local_side, mod_display_name, modrinth_side
from .runtime import install_runtime


@dataclass
class ModRecord:
    path: Path
    sha1: str
    display_name: str
    version: dict | None = None
    fingerprint: int | None = None
    curseforge_match: dict | None = None
    side: str = "review"
    reason: str = "No reliable server-side declaration found"
    decision_source: str = "unknown"

    def as_report_entry(self) -> dict:
        file_data = self.curseforge_match.get("file", {}) if self.curseforge_match else {}
        return {
            "filename": self.path.name,
            "name": self.display_name,
            "sha1": self.sha1,
            "side": self.side,
            "reason": self.reason,
            "decision_source": self.decision_source,
            "modrinth_project_id": self.version.get("project_id") if self.version else None,
            "modrinth_version_id": self.version.get("id") if self.version else None,
            "curseforge_mod_id": self.curseforge_match.get("id") if self.curseforge_match else None,
            "curseforge_file_id": file_data.get("id"),
        }


def _batches(items: list, size: int = BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _find_game_directory(client: Path) -> Path:
    if not client.is_dir():
        raise ValueError(f"Client modpack directory does not exist: {client}")
    candidates = [client, client / ".minecraft"]
    if (client / "mmc-pack.json").is_file():
        candidates.append(client / "minecraft")
    for candidate in candidates:
        mods = candidate / "mods"
        if mods.is_dir() and not mods.is_symlink():
            return candidate
    raise ValueError(f"No mods/ directory found in {client} or its Minecraft game folder")


def _load_rules(path: Path | None, filenames: set[str]) -> dict[str, str]:
    if path is None:
        return {}
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read rules file {path}: {error}") from error
    if not isinstance(rules, dict) or any(
        not isinstance(name, str) or side not in {"server", "client", "review"}
        for name, side in rules.items()
    ):
        raise ValueError("Rules must map JAR filenames to server, client, or review")
    unknown_names = set(rules) - filenames
    if unknown_names:
        raise ValueError(f"Rules refer to missing JARs: {', '.join(sorted(unknown_names))}")
    return rules


def _same_tree(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _matches_sha1(match: dict, sha1: str, size: int) -> bool:
    file_data = match.get("file")
    if not isinstance(file_data, dict):
        return False
    file_length = file_data.get("fileLength")
    if isinstance(file_length, int) and file_length > 0 and file_length != size:
        return False
    hashes = file_data.get("hashes")
    if not isinstance(hashes, list):
        return True
    sha1_values = [item.get("value", "").lower() for item in hashes
                   if isinstance(item, dict) and item.get("algo") == 1
                   and isinstance(item.get("value"), str)]
    return not sha1_values or sha1 in sha1_values


def _classify(record: ModRecord, rules: dict[str, str]) -> None:
    if record.path.name in rules:
        record.side = rules[record.path.name]
        record.reason = "Manual rule"
        record.decision_source = "rule"
        return
    remote = modrinth_side(record.version)
    if remote:
        record.side, record.reason = remote
        record.decision_source = "modrinth"
        return
    local = local_side(record.path)
    if local:
        record.side, record.reason = local
        record.decision_source = record.reason.split(" ", 1)[0]


def build(
    client_path: Path,
    output_path: Path | None = None,
    *,
    rules_path: Path | None = None,
    curseforge_api_key: str | None = None,
    api: ModAPI | None = None,
    minecraft_version: str | None = None,
    loader: str | None = None,
    loader_version: str | None = None,
    java: str = "java",
    confirm_eula: Callable[[], bool] | None = None,
    allow_unresolved: bool = False,
    runtime_installer: Callable = install_runtime,
    progress: Callable[[str], None] | None = None,
    mod_status: Callable[[str, str, str], None] | None = None,
    install_start: Callable[[str], None] | None = None,
    install_end: Callable[[bool], None] | None = None,
    review_spec: Callable[[ServerSpec | None], ServerSpec] | None = None,
    resolve_mod: Callable[[ModRecord], str] | None = None,
) -> dict:
    """Build the output directory and return its report.

    The source directory is only read. An existing output directory is refused.
    An explicit EULA confirmation is required before downloading the server.
    """
    client = client_path.expanduser().resolve()
    game = _find_game_directory(client)
    root = instance_root(client)
    output = (output_path.expanduser() if output_path else
              root.parent / f"{root.name}-server")
    if output.is_symlink():
        raise ValueError(f"Output already exists: {output}")
    output = output.resolve()
    if _same_tree(root, output):
        raise ValueError("Output must be outside the client modpack directory")
    if output.exists():
        raise ValueError(f"Output already exists: {output}")

    mods_directory = game / "mods"
    mods = sorted(
        (path for path in mods_directory.iterdir()
         if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".jar"),
        key=lambda path: path.name.lower(),
    )
    if not mods:
        raise ValueError(f"No JAR files found in {mods_directory}")

    def detect(versions: list[dict]) -> ServerSpec:
        return detect_server_spec(
            client, game, mods, versions, minecraft_version=minecraft_version,
            loader=loader, loader_version=loader_version,
        )

    spec = None
    if review_spec:
        try:
            spec = detect([])
        except ValueError:
            pass
        if spec is not None:
            spec = review_spec(spec)

    rules = _load_rules(rules_path, {path.name for path in mods})
    records = []
    for path in mods:
        name = mod_display_name(path)
        if mod_status:
            mod_status(path.name, name, "search")
        records.append(ModRecord(path=path, sha1=sha1_file(path), display_name=name))
    api = api or ModAPI(curseforge_api_key)
    warnings: list[str] = []

    for batch in _batches(records):
        try:
            versions = api.modrinth_versions([record.sha1 for record in batch])
        except APIError as error:
            warnings.append(f"Modrinth lookup failed for {len(batch)} JAR(s): {error}")
            continue
        for record in batch:
            record.version = versions.get(record.sha1)
            if mod_status and (record.path.name in rules or modrinth_side(record.version)):
                _classify(record, rules)
                mod_status(record.path.name, record.display_name, record.side)

    # Missing hashes and versions with no usable side information both go
    # through CurseForge's exact fingerprint lookup when a key is available.
    fallback = [record for record in records if modrinth_side(record.version) is None]
    if fallback and not api.curseforge_key:
        unresolved = [record for record in fallback
                      if record.path.name not in rules and local_side(record.path) is None]
        if unresolved:
            warnings.append(
                f"CurseForge lookup skipped for {len(unresolved)} JAR(s): "
                "CURSEFORGE_API_KEY is not set"
            )
    elif fallback:
        for record in fallback:
            record.fingerprint = curseforge_fingerprint(record.path)
        for batch in _batches(fallback):
            try:
                matches = api.curseforge_matches([record.fingerprint for record in batch])
            except APIError as error:
                warnings.append(f"CurseForge lookup failed for {len(batch)} JAR(s): {error}")
                continue
            for record in batch:
                for match in matches.get(record.fingerprint, []):
                    if _matches_sha1(match, record.sha1, record.path.stat().st_size):
                        record.curseforge_match = match
                        break

    for record in records:
        _classify(record, rules)
        if mod_status:
            mod_status(record.path.name, record.display_name, record.side)

    versions = [record.version for record in records if isinstance(record.version, dict)]
    if review_spec and spec is None:
        try:
            spec = detect(versions)
        except ValueError:
            pass
        spec = review_spec(spec)

    if resolve_mod:
        for record in records:
            if record.side == "review":
                record.side = resolve_mod(record)
                record.reason = "Selected in the interactive wizard"
                record.decision_source = "wizard"
                if mod_status:
                    mod_status(record.path.name, record.display_name, record.side)

    unresolved = [record.path.name for record in records if record.side == "review"]
    if unresolved and not allow_unresolved:
        raise ValueError(
            "Cannot build a ready-to-run server: unresolved mods: "
            + ", ".join(unresolved)
            + ". Add --rules rules.json with server/client decisions, or use "
            "--allow-unresolved to create an incomplete pack for review"
        )

    if spec is None:
        spec = detect(versions)
    if spec.loader_version is None:
        warnings.append(
            "Exact loader version was not found; the latest compatible release "
            "will be installed. Use --loader-version if the pack requires a specific version"
        )
    if confirm_eula is None or not confirm_eula():
        raise ValueError("Minecraft EULA was not accepted; type exactly 'I agree' to continue")

    report = {
        "source": str(client),
        "game_directory": str(game),
        "output": str(output),
        "target": "Linux x86_64",
        "ready": not unresolved,
        "eula_accepted": True,
        "server_core": {},
        "counts": {side: sum(record.side == side for record in records)
                   for side in ("server", "client", "review")},
        "mods": [record.as_report_entry() for record in records],
        "config_files": [],
        "warnings": warnings,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        if install_start:
            install_start(f"Installing Minecraft {spec.minecraft_version} "
                          f"({spec.loader})...")
        installed = False
        try:
            report["server_core"] = runtime_installer(spec, temporary, java)
            installed = True
        finally:
            if install_end:
                install_end(installed)
        if progress:
            progress("Copying server mods and config files...")
        report["server_core"]["detected_from"] = spec.detected_from
        (temporary / "mods").mkdir(exist_ok=True)
        report["config_files"] = copy_server_files(
            game, temporary, [(record.path, record.side) for record in records], warnings,
        )
        for record in records:
            if record.side == "server":
                destination = temporary / "mods" / record.path.name
            elif record.side == "review":
                destination = temporary / "review" / "mods" / record.path.name
            else:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(record.path, destination)

        (temporary / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        (temporary / "srvcrt-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (temporary / "README_SERVER.txt").write_text(
            "Server pack created by srvcrt.\n"
            f"Minecraft {spec.minecraft_version}, {spec.loader} "
            f"{report['server_core']['loader_version']}; "
            f"requires Java {report['server_core']['required_java']}+.\n"
            "Copy this entire directory to Linux x86_64 and run ./start.sh.\n"
            "Memory for Fabric/Quilt: JAVA_XMS and JAVA_XMX; for Forge/NeoForge: "
            "user_jvm_args.txt.\n"
            "Read srvcrt-report.json before starting the server.\n"
            + ("Incomplete pack: JAR files in review/mods need a decision.\n"
               if unresolved else ""),
            encoding="utf-8",
        )
        if output.exists():
            raise ValueError(f"Output already exists: {output}")
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return report
