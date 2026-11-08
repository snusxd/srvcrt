# srvcrt

`srvcrt` is a CLI tool for building ready-to-run Minecraft Java Edition servers from scratch or from an installed client modpack. It automatically resolves the required Java version, downloads a portable OpenJDK, installs the server core, and filters client-only mods and configs.

> [!WARNING]
> This project is completely vibecoded.

## Installation & Quick Start

Requires **Python 3.10+** and an internet connection.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

> [!NOTE]
> `srvcrt` automatically detects the Java version required by the selected Minecraft release (Java 8, 16, 17, 21, 25+) and downloads a portable OpenJDK (Eclipse Temurin / Azul Zulu) into `~/.cache/srvcrt/jdks/` and the server's `./jdk` directory.

### Interactive Wizard

Run `srvcrt` without `--client` to launch the interactive setup wizard:

```sh
srvcrt
srvcrt -o ./servers
```

1. **Choose source**: `New` (clean server) or `Client` (from an installed client pack).
2. **Select version & core**:
   - **Vanilla**: Mojang Vanilla, Paper, Purpur.
   - **Mods**: Fabric, NeoForge, Forge, Quilt.
3. **Accept EULA**: Type `I agree` to accept the [Minecraft EULA](https://www.minecraft.net/en-us/eula).
4. **Start the server**:

```sh
cd <server-directory>
./start.sh
```

## Direct CLI Usage (Client Modpack)

Build a server directly from an installed client instance containing `mods/` (or a parent folder with `.minecraft/`):

```sh
srvcrt --client "/path/to/.minecraft"
srvcrt --client "/path/to/.minecraft" --minecraft-version 1.21.1 --loader fabric --loader-version 0.17.3
```

### CLI Options

| Option | Description |
| --- | --- |
| `--client`, `-c`, `--input`, `-i <PATH>` | Path to the installed client pack directory |
| `--output`, `-o <PATH>` | Parent directory for the generated server folder (default: current directory) |
| `--minecraft-version <VERSION>` | Override or specify the Minecraft version |
| `--loader <LOADER>` | Specify mod loader (`fabric`, `neoforge`, `forge`, `quilt`) |
| `--loader-version <VERSION>` | Specify exact mod loader version (default: detected or latest compatible) |
| `--java <COMMAND>` | Override automatic portable JDK with a custom Java executable |
| `--rules <FILE>` | JSON file mapping JAR filenames to `server`, `client`, or `review` |
| `--allow-unresolved` | Place unclassified JARs in `review/mods/` instead of aborting |

## Mod Classification & Rules

When building from a client pack, `srvcrt` classifies every JAR in `mods/` as `Server-side`, `Client-side`, or `Needs review` using:

1. **Modrinth API** (by exact SHA-1 hash).
2. **CurseForge API** (by exact Murmur2 fingerprint, when `CURSEFORGE_API_KEY` is set in the environment).
3. **JAR Metadata** (`fabric.mod.json`, `quilt.mod.json`, `mods.toml`, `neoforge.mods.toml`).

> [!IMPORTANT]
> The client pack must already contain downloaded `.jar` files in `mods/`. A CurseForge `manifest.json` export alone is not sufficient.

To manually classify unresolved mods in non-interactive mode, provide a `--rules rules.json` file:

```json
{
  "example-mod.jar": "server",
  "visual-only.jar": "client"
}
```

## Output Structure

- `start.sh` — Launch script that automatically uses `./jdk` when compatible or falls back to `JAVA_BIN` / system `java` (`JAVA_XMS` and `JAVA_XMX` configure memory for Vanilla/Fabric/Quilt; `user_jvm_args.txt` for Forge/NeoForge).
- `jdk/` — Portable OpenJDK matched to the server's Minecraft version.
- `mods/` — Filtered server-side and shared mod JARs.
- `config/`, `defaultconfigs/`, `kubejs/`, `scripts/`, `datapacks/`, `global_packs/`, `openloader/`, `patchouli_books/` — Server and shared configs (client-only mod configs and `*-client` files are excluded).
- `eula.txt`, `README_SERVER.txt`, and `srvcrt-report.json` — Full build and classification audit report.

## Development & Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
