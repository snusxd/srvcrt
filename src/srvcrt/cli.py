"""Command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .builder import build
from .instance import instance_root
from .progress import ConsoleProgress
from .runtime import InstallError
from .wizard import run_wizard


class CLIParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        for arg in args:
            if arg == "--":
                break
            option = arg.partition("=")[0]
            if option.startswith("-") and not option.startswith("--"):
                long_option = "-" + option
                if long_option in self._option_string_actions:
                    # argparse can otherwise read -client as -c with value lient.
                    self.error(f"unrecognized argument {option}; use {long_option}")
        return super().parse_args(args, namespace)


def confirm_minecraft_eula() -> bool:
    print("Before downloading the server, read the Minecraft EULA:")
    print("https://www.minecraft.net/en-us/eula")
    try:
        return input('Type exactly "I agree" to accept: ') == "I agree"
    except EOFError:
        return False


def make_parser() -> argparse.ArgumentParser:
    parser = CLIParser(
        prog="srvcrt",
        description="Create a Minecraft server from scratch or from a client pack.",
        epilog="Run without --client to open the interactive setup wizard.",
        allow_abbrev=False,
    )
    parser.add_argument("--client", "-c", "--input", "-i", type=Path,
                        metavar="PATH", help="Client pack directory containing mods/")
    parser.add_argument("--output", "-o", type=Path, metavar="PATH",
                        help="Parent directory for the server folder (default: current directory)")
    parser.add_argument("--rules", type=Path, metavar="FILE",
                        help="JSON file mapping JAR names to server, client, or review")
    parser.add_argument("--minecraft-version", metavar="VERSION",
                        help="Minecraft version if it cannot be detected")
    parser.add_argument("--loader", choices=("forge", "neoforge", "fabric", "quilt"),
                        help="Mod loader if it cannot be detected")
    parser.add_argument("--loader-version", metavar="VERSION",
                        help="Loader version (detected or latest compatible by default)")
    parser.add_argument("--java", default="java", metavar="COMMAND",
                        help="Java executable used to install the server core")
    parser.add_argument("--allow-unresolved", action="store_true",
                        help="Place unknown JARs in review/mods; the pack may be incomplete")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    display = ConsoleProgress()
    try:
        parent = args.output.expanduser().absolute() if args.output is not None else Path.cwd()
        if args.client is None:
            if any((args.rules, args.minecraft_version, args.loader, args.loader_version,
                    args.allow_unresolved)):
                parser.error("client build options require --client")
            run_wizard(parent, args.java, display)
            return 0
        client = args.client.expanduser().resolve()
        report = build(
            client,
            parent / f"{instance_root(client).name}-server",
            rules_path=args.rules,
            curseforge_api_key=os.environ.get("CURSEFORGE_API_KEY"),
            minecraft_version=args.minecraft_version,
            loader=args.loader,
            loader_version=args.loader_version,
            java=args.java,
            confirm_eula=confirm_minecraft_eula,
            allow_unresolved=args.allow_unresolved,
            progress=display.message,
            mod_status=display.mod_status,
            install_start=display.start_install,
            install_end=display.finish_install,
        )
    except (OSError, ValueError, InstallError) as error:
        print(f"srvcrt: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nsrvcrt: cancelled by user", file=sys.stderr)
        return 130
    counts = report["counts"]
    print(f"Server pack: {report['output']}")
    print(f"Mods: {counts['server']} server-side, "
          f"{counts['client']} client-side, "
          f"{counts['review']} need review")
    core = report["server_core"]
    print(f"Core: Minecraft {core['minecraft_version']}, "
          f"{core['loader']} {core['loader_version']}; "
          f"Java {core['required_java']}+")
    print("Start on Linux: ./start.sh")
    if not report["ready"]:
        print("Pack is incomplete: check review/mods and the report", file=sys.stderr)
    print(f"Report: {Path(report['output']) / 'srvcrt-report.json'}")
    for warning in report["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)
    return 0
