"""Headless subcommands: the read-only reports and the safe batches, for cron.

`vboxfront health` from a crontab answers "is anything broken?" with an exit
code; `vboxfront compact-all` prints the script it would run unless told to
`--run` it — plan mode, as the default. Same parsers, same command builders
and the same media library as the window, so the two never disagree about
what is registered.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time

from .commands import list_media_args, plan_script
from .constants import KINDS
from .domain import (
    health_findings,
    health_report,
    is_inaccessible,
    parse_media_list,
    records_to_csv,
    records_to_json,
)
from .persistence import append_history, load_library, vboxmanage_path, vboxmanage_problem
from .reports import chain_of, chain_problems, space_report, space_report_json, space_report_text
from .trash import TrashStore, allocated_bytes
from .version import APP_VERSION

COMMANDS = ("list", "health", "space", "compact-all", "verify")
TIMEOUT = 120


class CliError(Exception):
    pass


def capture(vbm: str, args: list[str], timeout: int = TIMEOUT) -> tuple[int, str]:
    try:
        done = subprocess.run([vbm, *args], capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"VBoxManage did not answer within {timeout}s\n"
    except OSError as e:
        return 126, f"{e}\n"
    return done.returncode, (done.stdout + done.stderr).decode(errors="replace")


def require_vbm() -> str:
    problem = vboxmanage_problem()
    vbm = vboxmanage_path()
    if problem or not vbm:
        raise CliError(problem or "VBoxManage not found")
    return vbm


def load_media(vbm: str, log=None) -> dict[str, list]:
    """The registry as the window would show it, library entries included."""
    for kind in KINDS:
        for path in load_library(kind):
            code, _out = capture(vbm, ["showmediuminfo", kind, path])
            if code and log:
                log(f"[library] could not open {kind} {path} (exit {code})")
    media = {}
    for kind in KINDS:
        code, output = capture(vbm, list_media_args(kind))
        if code:
            raise CliError(f"`VBoxManage {' '.join(list_media_args(kind))}` failed "
                           f"(exit {code}): {output.strip()}")
        media[kind] = parse_media_list(output)
    return media


def cmd_list(args, vbm, out) -> int:
    media = load_media(vbm, log=lambda line: print(line, file=sys.stderr))
    kinds = KINDS if args.kind == "all" else [args.kind]
    records = [r for kind in kinds for r in media[kind]]
    out.write(records_to_csv(records) if args.csv else records_to_json(records))
    return 0


def cmd_health(args, vbm, out) -> int:
    findings = health_findings(load_media(vbm))
    if args.json:
        import json
        out.write(json.dumps([vars(f) for f in findings], indent=2) + "\n")
    else:
        out.write(health_report(findings))
    levels = {f.level for f in findings}
    if "error" in levels:
        return 1
    return 2 if args.strict and levels else 0


def cmd_space(args, vbm, out) -> int:
    trash = sum(allocated_bytes(e.trashed) or 0 for b in TrashStore().load() for e in b)
    sections = space_report(load_media(vbm), trash)
    if args.json:
        import json
        out.write(json.dumps(space_report_json(sections), indent=2) + "\n")
    else:
        out.write(space_report_text(sections))
    return 0


def cmd_compact_all(args, vbm, out) -> int:
    disks = [r for r in load_media(vbm)["disk"]
             if r.fmt.upper() == "VDI" and not is_inaccessible(r)]
    commands = [["modifymedium", "disk", d.uuid, "--compact"] for d in disks]
    if not args.run:
        out.write(plan_script(commands))
        return 0
    for command in commands:
        line = " ".join(shlex.quote(a) for a in ["VBoxManage", *command])
        out.write(f"$ {line}\n")
        out.flush()
        try:
            code = subprocess.run([vbm, *command], stdin=subprocess.DEVNULL).returncode
        except OSError as e:
            out.write(f"[error] {e}\n")
            code = 126
        append_history(line, code, time.strftime("%Y-%m-%d %H:%M:%S"))
        if code:
            out.write(f"[queue] stopped: exit {code}\n")
            return code
    out.write(f"[done] compacted {len(commands)} disk(s)\n")
    return 0


def cmd_verify(args, vbm, out) -> int:
    from .verify import VerifySource, VerifyTask, verdict
    from .domain import parse_capacity_mb

    media = load_media(vbm)
    known = {}
    for kind in KINDS:
        for rec in media[kind]:
            known[rec.uuid] = (kind, rec)
            known[rec.location] = (kind, rec)

    def source(token: str, is_file: bool) -> VerifySource:
        if is_file:
            return VerifySource(token, token, is_file=True)
        if token not in known:
            raise CliError(f"{token} is not a registered medium (pass --file for a flat image)")
        kind, rec = known[token]
        problems = chain_problems(chain_of(media[kind], rec.uuid))
        if problems:
            raise CliError(f"{token}: " + "; ".join(problems))
        return VerifySource(token, rec.uuid, kind,
                            expected_bytes=(parse_capacity_mb(rec.capacity) or 0) * 1024 * 1024)

    sources = [source(args.a, False), source(args.b, args.file)]
    sources[1].expected_bytes = sources[1].expected_bytes or sources[0].expected_bytes
    task = VerifyTask(vbm, sources)
    results = []
    task.done.connect(results.extend)
    for s in sources:
        out.write(f"[verify] {s.describe()}\n")
    task.run()  # synchronously, in this thread
    identical, text = verdict(results)
    out.write(text + "\n")
    return 0 if identical else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vboxfront",
        description="VBoxFront without the window. Run with no arguments for the GUI.")
    parser.add_argument("--version", action="version", version=f"vboxfront {APP_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="registered media as JSON (default) or CSV")
    p.add_argument("--kind", choices=[*KINDS, "all"], default="all")
    p.add_argument("--csv", action="store_true")
    p = sub.add_parser("health", help="registry problems; exit 1 on errors")
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict", action="store_true", help="exit 2 when there are only warnings")
    p = sub.add_parser("space", help="where the disk space goes")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("compact-all", help="compact every accessible VDI (prints the plan "
                                           "unless --run)")
    p.add_argument("--run", action="store_true", help="run the commands, stopping at the "
                                                      "first failure")
    p = sub.add_parser("verify", help="compare two images' contents; exit 1 when they differ")
    p.add_argument("a", help="registered medium (UUID or path)")
    p.add_argument("b", help="registered medium, or a flat image with --file")
    p.add_argument("--file", action="store_true", help="B is a flat image file")
    return parser


HANDLERS = {"list": cmd_list, "health": cmd_health, "space": cmd_space,
            "compact-all": cmd_compact_all, "verify": cmd_verify}


def wants_cli(argv: list[str]) -> bool:
    return len(argv) > 1 and (argv[1] in COMMANDS
                              or argv[1] in ("-h", "--help", "--version", "-V"))


def main(argv: list[str] | None = None, out=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = out or sys.stdout
    if argv[:1] == ["-V"]:
        argv = ["--version"]
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args, require_vbm(), out)
    except CliError as e:
        print(f"vboxfront: {e}", file=sys.stderr)
        return 3
