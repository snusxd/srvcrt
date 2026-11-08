import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from srvcrt.api import APIError, CURSEFORGE_URL, MODRINTH_URL, ModAPI
from srvcrt.builder import build
from srvcrt.cli import confirm_minecraft_eula, main, make_parser
from srvcrt.fingerprint import curseforge_fingerprint, sha1_file
from srvcrt.instance import detect_server_spec
from srvcrt.progress import ConsoleProgress
from srvcrt.runtime import InstallError, _installer_url


def fake_runtime(spec, directory, java):
    (directory / "server.jar").write_bytes(b"server core")
    (directory / "start.sh").write_text("#!/bin/sh\n")
    return {"minecraft_version": spec.minecraft_version, "loader": spec.loader,
            "loader_version": spec.loader_version or "0.19.0",
            "required_java": 21, "start_script": "start.sh"}


def make_jar(path: Path, fabric_environment: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as jar:
        if fabric_environment is not None:
            jar.writestr("fabric.mod.json", json.dumps({
                "schemaVersion": 1,
                "id": path.stem,
                "version": "1.0.0",
                "environment": fabric_environment,
            }))
        else:
            jar.writestr("META-INF/mods.toml", 'modLoader="javafml"\n')


class FakeAPI:
    def __init__(self, versions=None, matches=None, *, fail_modrinth=False, key=None):
        self.versions = versions or {}
        self.matches = matches or {}
        self.fail_modrinth = fail_modrinth
        self.curseforge_key = key
        self.modrinth_calls = []
        self.curseforge_calls = []

    def modrinth_versions(self, hashes):
        self.modrinth_calls.append(hashes)
        if self.fail_modrinth:
            raise APIError("offline")
        return {value: self.versions[value] for value in hashes if value in self.versions}

    def curseforge_matches(self, fingerprints):
        self.curseforge_calls.append(fingerprints)
        return {value: self.matches[value] for value in fingerprints if value in self.matches}


class FingerprintTests(unittest.TestCase):
    def test_curseforge_reference_vector_and_whitespace(self):
        # Cross-checked against Aternos's CursedFingerprintHelper (seed 1).
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mod.jar"
            with patch("srvcrt.fingerprint.CHUNK_SIZE", 2):
                path.write_bytes(b"foo")
                self.assertEqual(curseforge_fingerprint(path), 197930586)
                path.write_bytes(b"fo o\n\r\t")
                self.assertEqual(curseforge_fingerprint(path), 197930586)
                path.write_bytes(b"fo obar\nbaz")
                self.assertEqual(curseforge_fingerprint(path), 2517958894)

    def test_forge_explicit_client_side_only(self):
        from srvcrt.metadata import local_side

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.jar"
            with zipfile.ZipFile(path, "w") as jar:
                jar.writestr("META-INF/mods.toml",
                             "modLoader=\"javafml\"\nclientSideOnly = true # comment\n[[mods]]\n")
            self.assertEqual(local_side(path),
                             ("client", "mods.toml clientSideOnly: true"))

    def test_quilt_and_neoforge_local_side(self):
        from srvcrt.metadata import local_side

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quilt = root / "quilt.jar"
            neo = root / "neoforge.jar"
            with zipfile.ZipFile(quilt, "w") as jar:
                jar.writestr("quilt.mod.json", json.dumps({
                    "quilt_loader": {"id": "quilt", "environment": "client"},
                }))
            with zipfile.ZipFile(neo, "w") as jar:
                jar.writestr("META-INF/neoforge.mods.toml",
                             "modLoader=\"javafml\"\nclientSideOnly=true\n")
            self.assertEqual(local_side(quilt),
                             ("client", "quilt.mod.json environment: client"))
            self.assertEqual(local_side(neo),
                             ("client", "neoforge.mods.toml clientSideOnly: true"))

    def test_quilt_metadata_accepts_comments_and_trailing_commas(self):
        from srvcrt.configs import mod_ids
        from srvcrt.metadata import local_side

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visual.jar"
            with zipfile.ZipFile(path, "w") as jar:
                jar.writestr("quilt.mod.json", '''{
                    // This is a common Quilt metadata style.
                    "quilt_loader": {
                        "id": "visual",
                        "environment": "client",
                        "homepage": "https://example.com",
                    },
                }''')
            self.assertEqual(local_side(path),
                             ("client", "quilt.mod.json environment: client"))
            self.assertEqual(mod_ids(path), {"visual"})

    def test_display_name_from_jar_metadata_or_filename(self):
        from srvcrt.metadata import mod_display_name

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fabric = root / "fabric-1.0.jar"
            forge = root / "forge-1.0.jar"
            fallback = root / "unknown-1.0.jar"
            with zipfile.ZipFile(fabric, "w") as jar:
                jar.writestr("fabric.mod.json", json.dumps({
                    "id": "fabricmod", "name": "Fabric World Tools",
                }))
            with zipfile.ZipFile(forge, "w") as jar:
                jar.writestr("META-INF/mods.toml",
                             '[[mods]]\nmodId="forgemod"\ndisplayName="Forge World Tools"\n')
            with zipfile.ZipFile(fallback, "w"):
                pass
            self.assertEqual(mod_display_name(fabric), "Fabric World Tools")
            self.assertEqual(mod_display_name(forge), "Forge World Tools")
            self.assertEqual(mod_display_name(fallback), "unknown-1.0")


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / "client"
        (self.client / "mods").mkdir(parents=True)
        (self.client / "mmc-pack.json").write_text(json.dumps({"components": [
            {"uid": "net.minecraft", "version": "1.21.1"},
            {"uid": "net.fabricmc.fabric-loader", "version": "0.17.3"},
        ]}))

    def build(self, **kwargs):
        return build(self.client, runtime_installer=fake_runtime,
                     confirm_eula=lambda: True, **kwargs)

    def test_modrinth_classification_and_curseforge_fallback(self):
        mods = self.client / "mods"
        server = mods / "server.jar"
        client = mods / "client.jar"
        fallback = mods / "fallback.jar"
        unknown = mods / "unknown.jar"
        for path, environment in (
            (server, "*"), (client, "client"),
            (fallback, "client"), (unknown, None),
        ):
            make_jar(path, environment)
        (self.client / "config").mkdir()
        (self.client / "config" / "example.toml").write_text("setting=true")

        fingerprint = curseforge_fingerprint(fallback)
        fallback_match = {"id": 123, "file": {
            "id": 456, "fileFingerprint": fingerprint,
            "fileLength": fallback.stat().st_size,
            "hashes": [{"algo": 1, "value": sha1_file(fallback)}],
        }}
        api = FakeAPI(
            versions={
                sha1_file(server): {"id": "v1", "project_id": "p1",
                                    "environment": "client_and_server"},
                sha1_file(client): {"id": "v2", "project_id": "p2",
                                    "environment": "client_only"},
            },
            matches={fingerprint: [fallback_match]}, key="test-key",
        )
        report = self.build(api=api, allow_unresolved=True)
        output = self.root / "client-server"

        self.assertEqual(report["counts"], {"server": 1, "client": 2, "review": 1})
        self.assertTrue((output / "mods" / "server.jar").exists())
        self.assertFalse((output / "mods" / "client.jar").exists())
        self.assertFalse((output / "mods" / "fallback.jar").exists())
        self.assertTrue((output / "review" / "mods" / "unknown.jar").exists())
        self.assertTrue((output / "config" / "example.toml").exists())
        self.assertTrue((output / "server.jar").exists())
        self.assertEqual((output / "eula.txt").read_text(), "eula=true\n")
        self.assertFalse(report["ready"])
        self.assertTrue(client.exists())
        self.assertEqual(len(api.curseforge_calls), 1)
        entries = {entry["filename"]: entry for entry in report["mods"]}
        self.assertEqual(entries["fallback.jar"]["curseforge_mod_id"], 123)
        self.assertEqual(entries["fallback.jar"]["decision_source"], "fabric.mod.json")
        self.assertEqual(json.loads((output / "srvcrt-report.json").read_text()), report)

    def test_modrinth_failure_uses_curseforge_then_local_metadata(self):
        path = self.client / "mods" / "both.jar"
        make_jar(path, "*")
        fingerprint = curseforge_fingerprint(path)
        api = FakeAPI(
            fail_modrinth=True,
            matches={fingerprint: [{"id": 98, "file": {
                "id": 99, "fileFingerprint": fingerprint,
                "hashes": [{"algo": 1, "value": sha1_file(path)}],
            }}]},
            key="test-key",
        )
        report = self.build(api=api)
        self.assertEqual(report["counts"]["server"], 1)
        self.assertTrue(api.curseforge_calls)
        self.assertTrue(any("Modrinth lookup failed" in item for item in report["warnings"]))
        self.assertTrue((self.root / "client-server" / "mods" / "both.jar").exists())

    def test_manual_rule_overrides_unknown_and_existing_output_is_refused(self):
        path = self.client / "mods" / "forge.jar"
        make_jar(path)
        rules = self.root / "rules.json"
        rules.write_text('{"forge.jar": "server"}')
        api = FakeAPI()
        report = self.build(rules_path=rules, api=api)
        self.assertEqual(report["counts"]["server"], 1)
        self.assertFalse(report["warnings"])
        self.assertTrue((self.root / "client-server" / "mods" / "forge.jar").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build(rules_path=rules, api=api)

    def test_unknown_mod_blocks_ready_pack_before_eula(self):
        make_jar(self.client / "mods" / "unknown.jar")
        confirmed = []
        with self.assertRaisesRegex(ValueError, "unresolved mods: unknown.jar"):
            build(self.client, api=FakeAPI(), runtime_installer=fake_runtime,
                  confirm_eula=lambda: confirmed.append(True))
        self.assertEqual(confirmed, [])
        self.assertFalse((self.root / "client-server").exists())

    def test_eula_must_be_explicit(self):
        make_jar(self.client / "mods" / "server.jar", "*")
        with self.assertRaisesRegex(ValueError, "EULA was not accepted"):
            build(self.client, api=FakeAPI(), runtime_installer=fake_runtime,
                  confirm_eula=lambda: False)
        self.assertFalse((self.root / "client-server").exists())

    def test_client_config_is_filtered_and_server_config_kept(self):
        make_jar(self.client / "mods" / "visual.jar", "client")
        make_jar(self.client / "mods" / "world.jar", "*")
        config = self.client / "config"
        config.mkdir()
        for name in ("visual.json", "world.json", "world-client.toml", "shared.toml"):
            (config / name).write_text(name)
        report = self.build(api=FakeAPI())
        output = self.root / "client-server"
        self.assertFalse((output / "config" / "visual.json").exists())
        self.assertTrue((output / "config" / "world.json").exists())
        self.assertFalse((output / "config" / "world-client.toml").exists())
        self.assertTrue((output / "config" / "shared.toml").exists())
        self.assertTrue(report["ready"])

    def test_installer_failure_leaves_no_output(self):
        make_jar(self.client / "mods" / "server.jar", "*")
        starts = []
        endings = []

        def fail_install(spec, directory, java):
            (directory / "partial.jar").write_bytes(b"partial")
            raise InstallError("download failed")

        with self.assertRaisesRegex(InstallError, "download failed"):
            build(self.client, api=FakeAPI(), runtime_installer=fail_install,
                  confirm_eula=lambda: True,
                  install_start=starts.append, install_end=endings.append)
        self.assertFalse((self.root / "client-server").exists())
        self.assertIn("Installing Minecraft", starts[0])
        self.assertEqual(endings, [False])

    def test_mod_statuses_start_with_search_and_finish_with_side(self):
        make_jar(self.client / "mods" / "server.jar", "*")
        make_jar(self.client / "mods" / "client.jar", "client")
        make_jar(self.client / "mods" / "unknown.jar")
        events = []
        self.build(api=FakeAPI(), allow_unresolved=True,
                   mod_status=lambda filename, name, status:
                   events.append((filename, name, status)))
        self.assertEqual(events[:3], [
            ("client.jar", "client", "search"),
            ("server.jar", "server", "search"),
            ("unknown.jar", "unknown", "search"),
        ])
        final = {filename: status for filename, _, status in events}
        self.assertEqual(final, {"client.jar": "client", "server.jar": "server",
                                 "unknown.jar": "review"})


class InstanceTests(unittest.TestCase):
    def test_detects_all_four_loaders_from_mmc_pack(self):
        uids = {"fabric": "net.fabricmc.fabric-loader",
                "forge": "net.minecraftforge", "neoforge": "net.neoforged",
                "quilt": "org.quiltmc.quilt-loader"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for loader, uid in uids.items():
                (root / "mmc-pack.json").write_text(json.dumps({"components": [
                    {"uid": "net.minecraft", "version": "1.21.1"},
                    {"uid": uid, "version": "1.2.3"},
                ]}))
                spec = detect_server_spec(root, root, [], [])
                self.assertEqual((spec.minecraft_version, spec.loader,
                                  spec.loader_version), ("1.21.1", loader, "1.2.3"))

    def test_override_minecraft_discards_old_loader_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mmc-pack.json").write_text(json.dumps({"components": [
                {"uid": "net.minecraft", "version": "1.21.1"},
                {"uid": "net.fabricmc.fabric-loader", "version": "0.17.3"},
            ]}))
            spec = detect_server_spec(root, root, [], [], minecraft_version="1.20.1")
            self.assertIsNone(spec.loader_version)

    def test_detects_selected_launcher_profile_in_minecraft_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory) / ".minecraft"
            game.mkdir()
            (game / "launcher_profiles.json").write_text(json.dumps({
                "selectedProfile": "forge",
                "profiles": {
                    "forge": {"lastVersionId": "1.20.1-forge-47.2.0"},
                    "vanilla": {"lastVersionId": "1.21.1"},
                },
            }))
            spec = detect_server_spec(game, game, [], [])
            self.assertEqual((spec.minecraft_version, spec.loader,
                              spec.loader_version), ("1.20.1", "forge", "47.2.0"))

    def test_paired_mod_metadata_beats_unrelated_vanilla_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launcher_profiles.json").write_text(json.dumps({
                "selectedProfile": "vanilla",
                "profiles": {"vanilla": {"lastVersionId": "1.21.1"}},
            }))
            versions = [{"game_versions": ["1.20.1"], "loaders": ["forge"]}]
            spec = detect_server_spec(root, root, [], versions)
            self.assertEqual((spec.minecraft_version, spec.loader), ("1.20.1", "forge"))


class RuntimeTests(unittest.TestCase):
    def test_installer_urls_for_forge_and_legacy_neoforge(self):
        forge, _ = _installer_url("forge", "1.20.1", "1.20.1-47.2.0")
        neo, _ = _installer_url("neoforge", "1.20.1", "47.1.106")
        self.assertTrue(forge.endswith("/forge-1.20.1-47.2.0-installer.jar"))
        self.assertTrue(neo.endswith("/forge-1.20.1-47.1.106-installer.jar"))

    def test_minecraft_java_major_detection(self):
        from srvcrt.runtime import _minecraft_java_major

        self.assertEqual(_minecraft_java_major("1.12.2", {}), 8)
        self.assertEqual(_minecraft_java_major("1.16.5", {}), 8)
        self.assertEqual(_minecraft_java_major("1.17.1", {}), 16)
        self.assertEqual(_minecraft_java_major("1.20.1", {}), 17)
        self.assertEqual(_minecraft_java_major("1.20.4", {}), 17)
        self.assertEqual(_minecraft_java_major("1.20.5", {}), 21)
        self.assertEqual(_minecraft_java_major("1.21.1", {}), 21)
        self.assertEqual(_minecraft_java_major("26.3", {}), 25)
        self.assertEqual(
            _minecraft_java_major("1.20.1", {"javaVersion": {"majorVersion": 21}}),
            21,
        )

    def test_ensure_java_downloads_and_caches_portable_jdk_when_missing(self):
        import hashlib
        import tarfile
        from srvcrt.runtime import _write_start_script, ensure_java

        archive_bytes = io.BytesIO()
        script = b'#!/bin/sh\necho \'openjdk version "21.0.5"\' >&2\n'
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as tf:
            info = tarfile.TarInfo("jdk-21.0.5+11/bin/java")
            info.size = len(script)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(script))
        payload = archive_bytes.getvalue()
        sha256 = hashlib.sha256(payload).hexdigest()

        downloads = []

        def fake_download(url, destination, expected, algorithm="sha1", *, progress=None):
            downloads.append((url, expected, algorithm))
            destination.write_bytes(payload)
            return expected

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_dir = root / "cache"
            server1 = root / "server1"
            server2 = root / "server2"
            server1.mkdir()
            server2.mkdir()

            with (
                patch("srvcrt.runtime._host_jdk_platform",
                      return_value=("linux", "x64", "linux", "x86", "tar.gz")),
                patch("srvcrt.runtime._get_json", return_value=[{
                    "binary": {"package": {
                        "link": "https://example.org/OpenJDK21.tar.gz",
                        "checksum": sha256,
                    }},
                }]),
                patch("srvcrt.runtime._download_checked", side_effect=fake_download),
            ):
                java1 = ensure_java("java", 21, server1, cache_dir=cache_dir)
                java2 = ensure_java("java", 21, server2, cache_dir=cache_dir)

            self.assertEqual(len(downloads), 1)
            self.assertEqual(Path(java1), server1 / "jdk" / "bin" / "java")
            self.assertEqual(Path(java2), server2 / "jdk" / "bin" / "java")
            self.assertTrue((server1 / "jdk" / "bin" / "java").is_file())
            self.assertTrue((server2 / "jdk" / "bin" / "java").is_file())

            (server1 / "server.jar").write_bytes(b"jar")
            _write_start_script("vanilla", server1)
            start_text = (server1 / "start.sh").read_text()
            self.assertIn("./jdk/bin/java", start_text)
            self.assertIn("export JAVA_HOME=", start_text)

    def test_portable_jdk_falls_back_to_azul_when_adoptium_has_no_asset(self):
        from srvcrt.runtime import _portable_jdk_download

        sha256 = "b" * 64

        def fake_get_json(url):
            if "adoptium" in url:
                return []
            return [{
                "download_url": "https://cdn.azul.com/zulu/bin/zulu8-macosx_aarch64.tar.gz",
                "sha256_hash": sha256,
            }]

        with (
            patch("srvcrt.runtime._host_jdk_platform",
                  return_value=("mac", "aarch64", "macos", "arm", "tar.gz")),
            patch("srvcrt.runtime._get_json", side_effect=fake_get_json),
        ):
            link, checksum, ext = _portable_jdk_download(8)
        self.assertEqual(link, "https://cdn.azul.com/zulu/bin/zulu8-macosx_aarch64.tar.gz")
        self.assertEqual(checksum, sha256)
        self.assertEqual(ext, "tar.gz")


class EULATests(unittest.TestCase):
    def test_requires_exact_phrase(self):
        with patch("builtins.input", return_value="I agree"):
            self.assertTrue(confirm_minecraft_eula())
        with patch("builtins.input", return_value="i agree"):
            self.assertFalse(confirm_minecraft_eula())


class CLITests(unittest.TestCase):
    def test_input_and_output_aliases(self):
        for client_option in ("--client", "-c", "--input", "-i"):
            for output_option in ("--output", "-o"):
                with self.subTest(client=client_option, output=output_option):
                    args = make_parser().parse_args(
                        [client_option, "/tmp/client", output_option, "/tmp/server"]
                    )
                    self.assertEqual(args.client, Path("/tmp/client"))
                    self.assertEqual(args.output, Path("/tmp/server"))

    def test_options_with_wrong_number_of_dashes_are_rejected(self):
        for options in (("-client", "/tmp/client"),
                        ("-input=/tmp/client",),
                        ("--client", "/tmp/client", "-output", "/tmp/server"),
                        ("--c", "/tmp/client"),
                        ("--i", "/tmp/client"),
                        ("--client", "/tmp/client", "--o", "/tmp/server")):
            with self.subTest(options=options), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    make_parser().parse_args(options)
                self.assertEqual(error.exception.code, 2)

    def test_wizard_creates_vanilla_server_without_client(self):
        details = {"javaVersion": {"majorVersion": 21}, "downloads": {"server": {
            "url": "https://example.org/server.jar", "sha1": "a" * 40,
        }}}

        def download(url, destination, checksum, algorithm, *, progress):
            progress(1, 2)
            destination.write_bytes(b"server core")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "My server"
            (Path(directory) / "taken").mkdir()
            with (patch("builtins.input",
                        side_effect=["1", "../outside", "taken", "My server", "", "1", "1",
                                     "wrong", "", "I agree"]),
                  patch("srvcrt.wizard.Path.cwd", return_value=Path(directory)),
                  patch("srvcrt.wizard.runtime._latest_minecraft_version", return_value="1.21.1"),
                  patch("srvcrt.wizard.runtime._minecraft_details", return_value=details),
                  patch("srvcrt.wizard._paper_jar", return_value=None),
                  patch("srvcrt.wizard._purpur_jar", return_value=None),
                  patch("srvcrt.wizard.runtime.ensure_java", return_value="java"),
                  patch("srvcrt.wizard.runtime._download_checked", side_effect=download),
                  redirect_stdout(io.StringIO()) as console):
                self.assertEqual(main([]), 0)
            lines = console.getvalue().splitlines()
            headings = [line.index("Choose server") for line in lines if "Choose server" in line]
            self.assertEqual(headings, [9, 9, 9, 9])
            self.assertIn("Choose core version [latest]:", console.getvalue())
            self.assertIn("Version does not exist!", console.getvalue())
            self.assertIn('\n\n  000%   Downloading core...', console.getvalue())
            self.assertTrue(any("100%" in line and "Core downloaded." in line for line in lines))
            self.assertEqual((output / "server.jar").read_bytes(), b"server core")
            self.assertEqual((output / "eula.txt").read_text(), "eula=true\n")
            self.assertIn("-jar server.jar nogui", (output / "start.sh").read_text())

    def test_wizard_cleans_staging_when_install_is_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "servers"
            with (patch("builtins.input",
                        side_effect=["1", "My server", "1.21.1", "2", "1", "", "I agree"]),
                  patch("srvcrt.wizard.runtime._minecraft_details", return_value={}),
                  patch("srvcrt.wizard._available_cores", return_value={"Fabric": "0.19.0"}),
                  patch("srvcrt.wizard.runtime.install_runtime", side_effect=KeyboardInterrupt),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(main(["-o", str(parent)]), 130)
            self.assertEqual(list(parent.iterdir()), [])

    def test_client_wizard_resolves_unknown_mod_and_builds_server(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Path(directory) / "client instance"
            launch_dir = Path(directory) / "launch"
            launch_dir.mkdir()
            game = client / "minecraft"
            (game / "mods").mkdir(parents=True)
            make_jar(game / "mods" / "unknown.jar")
            (client / "mmc-pack.json").write_text(json.dumps({"components": [
                {"uid": "net.minecraft", "version": "1.21.1"},
                {"uid": "net.fabricmc.fabric-loader", "version": "0.17.3"},
            ]}))

            def install(spec, staging, java, *, progress):
                progress(35)
                return fake_runtime(spec, staging, java)

            with (patch("builtins.input", side_effect=["2", f"'{game}'", "", "4", "1", "I agree"]),
                  patch("srvcrt.cli.Path.cwd", return_value=launch_dir),
                  patch("srvcrt.builder.ModAPI", return_value=FakeAPI()),
                  patch("srvcrt.wizard.runtime.install_runtime", side_effect=install),
                  redirect_stdout(io.StringIO()) as console, redirect_stderr(io.StringIO())):
                self.assertEqual(main([]), 0)

            output = launch_dir / f"{client.name}-server"
            report = json.loads((output / "srvcrt-report.json").read_text())
            self.assertEqual(report["mods"][0]["decision_source"], "wizard")
            self.assertTrue((output / "mods" / "unknown.jar").is_file())
            self.assertIn("dir   Enter client instance path:", console.getvalue())
            self.assertIn('\n\n  000%   Downloading core...', console.getvalue())
            self.assertIn("Server-side: 1", console.getvalue())
            self.assertIn("Minecraft server created!", console.getvalue())


class ProgressTests(unittest.TestCase):
    def test_plain_output_shows_mod_statuses_and_braille_spinner(self):
        stream = io.StringIO()
        display = ConsoleProgress(stream)
        display.mod_status("visual.jar", "Visual Effects", "search")
        display.mod_status("visual.jar", "Visual Effects", "client")
        display.start_install("Installing Minecraft 1.21.1 (fabric)...")
        display.finish_install(True)
        output = stream.getvalue()
        self.assertIn("Visual Effects [Search]", output)
        self.assertIn("Visual Effects [Client-side]", output)
        self.assertIn("⠋ Installing Minecraft 1.21.1", output)
        self.assertIn("Minecraft server core installed.", output)

    def test_tty_rewrites_status_line_in_place(self):
        class TTYStream(io.StringIO):
            def isatty(self):
                return True

        stream = TTYStream()
        display = ConsoleProgress(stream)
        display.mod_status("world.jar", "World Tools", "search")
        display.mod_status("visual.jar", "Visual Effects", "search")
        display.mod_status("world.jar", "World Tools", "server")
        display.mod_status("visual.jar", "Visual Effects", "client")
        display.start_install("Installing Minecraft...")
        display.finish_install(True)
        output = stream.getvalue()
        self.assertIn("World Tools [Search]\n", output)
        self.assertIn("\x1b[2A\r\x1b[2KWorld Tools [Server-side]", output)
        self.assertIn("\x1b[1A\r\x1b[2KVisual Effects [Client-side]", output)
        self.assertIn("⠋ Installing Minecraft...", output)


class APITests(unittest.TestCase):
    def test_api_request_contracts(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            if request.full_url == MODRINTH_URL:
                return io.BytesIO(b'{"abc": {"environment": "client_only"}}')
            return io.BytesIO(b'{"data": {"exactMatches": [{"id": 12, '
                              b'"file": {"id": 34, "fileFingerprint": 42}}]}}')

        with patch("srvcrt.api.urlopen", side_effect=fake_urlopen):
            api = ModAPI("test-key")
            self.assertEqual(api.modrinth_versions(["abc"])["abc"]["environment"],
                             "client_only")
            self.assertEqual(api.curseforge_matches([42])[42][0]["id"], 12)
        self.assertEqual(json.loads(requests[0].data),
                         {"hashes": ["abc"], "algorithm": "sha1"})
        self.assertEqual(json.loads(requests[1].data), {"fingerprints": [42]})
        self.assertEqual(requests[1].get_header("X-api-key"), "test-key")


if __name__ == "__main__":
    unittest.main()
