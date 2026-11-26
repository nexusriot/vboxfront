"""Headless subcommands: the read-only reports and the safe batches, for cron.

`vboxfront health` from a crontab answers "is anything broken?" with an exit
code; `vboxfront compact-all` prints the script it would run unless told to
`--run` it — plan mode, as the default. Same parsers, same command builders
and the same media library as the window, so the two never disagree about
what is registered.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

from . import bundles, settings_io
from .chains import STATUS_TEXT, chain_fixes, chain_is_repairable, trace_chain
from .commands import list_media_args, plan_script, setlocation_args
from .constants import KINDS
from .domain import (
    discard_fix_plan,
    health_findings,
    health_report,
    history_for,
    is_inaccessible,
    is_uuid,
    parse_capacity_mb,
    parse_machinereadable,
    parse_media_list,
    parse_vms_list,
    same_file,
    space_warning,
    records_to_csv,
    records_to_json,
)
from .locate import (
    MATCH_UUID,
    default_search_folders,
    find_moved_image,
    parent_first,
    read_image_identity,
)
from .models import MediumRecord
from .persistence import (
    add_library_path,
    append_history,
    image_folder,
    load_history,
    load_library,
    vboxmanage_path,
    vboxmanage_problem,
)
from .reports import chain_of, chain_problems, space_report, space_report_json, space_report_text
from .trash import TrashStore, allocated_bytes
from .version import APP_VERSION

COMMANDS = ("list", "health", "space", "compact-all", "verify", "locate", "chain",
            "history", "fix-discard", "backup", "restore", "verify-bundle", "settings")
#: Commands that work from files and settings alone, with no VBoxManage.
OFFLINE = ("history", "verify-bundle", "settings")
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
        paths = load_library(kind)
        for path in (parent_first(paths) if kind == "disk" else paths):
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
    media = load_media(vbm)
    findings = health_findings(media)
    if args.fix_moved:
        return fix_moved(args, vbm, media, findings, out)
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
    code = run_commands(vbm, commands, out)
    if not code:
        out.write(f"[done] compacted {len(commands)} disk(s)\n")
    return code


def run_commands(vbm, commands: list[list[str]], out) -> int:
    """Run in order, echoing each, stopping at the first failure."""
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


def resolve_medium(media: dict, token: str):
    """(kind, record) for a registered UUID or path, or CliError."""
    want = token.strip("{}").lower()
    for kind in KINDS:
        for rec in media[kind]:
            if rec.uuid.lower() == want or (rec.location and same_file(rec.location, token)):
                return kind, rec
    raise CliError(f"{token} is not a registered medium")


def search_folders(args, location: str) -> list[str]:
    folders = list(args.folders or [])
    if not folders:
        folders = default_search_folders(location, [image_folder()])
    return folders


def fix_moved(args, vbm, media, findings, out) -> int:
    """Search for every missing file by UUID; print (or run) the re-points."""
    known = [r.location for kind in KINDS for r in media[kind]]
    for kind in KINDS:
        known += load_library(kind)
    by_uuid = {r.uuid: (kind, r) for kind in KINDS for r in media[kind]}
    commands, unresolved = [], []
    seen = set()
    for f in findings:
        if f.code not in ("missing-file", "inaccessible") or f.uuid in seen:
            continue
        if f.location and os.path.exists(f.location):
            continue  # damaged, not moved: Resolve's repair, not a re-point
        seen.add(f.uuid)
        kind, rec = by_uuid[f.uuid]
        found = [c for c in find_moved_image(
            rec.uuid, rec.location, search_folders(args, rec.location), known,
            parse_capacity_mb(rec.capacity)) if c.match == MATCH_UUID and not c.note]
        name = os.path.basename(rec.location) or rec.uuid
        if len(found) == 1:
            commands.append(setlocation_args(kind, rec.uuid, found[0].path))
        else:
            unresolved.append(f"# {name}: " + ("not found" if not found else
                              f"{len(found)} files carry its UUID; choose with Resolve"))
    if not args.run:
        out.write(plan_script(commands) if commands else "# nothing to re-point\n")
        for line in unresolved:
            out.write(line + "\n")
        return 1 if unresolved else 0
    code = run_commands(vbm, commands, out)
    for line in unresolved:
        out.write(line + "\n")
    return code or (1 if unresolved else 0)


def cmd_locate(args, vbm, out) -> int:
    token = args.medium
    location, uuid, size = "", token, None
    if not is_uuid(token):
        identity = read_image_identity(token)
        if identity is None:
            raise CliError(f"{token}: not a UUID, and no UUID header to read")
        uuid, location = identity.uuid, token
    media = load_media(vbm)
    known = [r.location for kind in KINDS for r in media[kind]]
    try:
        _kind, rec = resolve_medium(media, uuid)
        location, size = rec.location, parse_capacity_mb(rec.capacity)
    except CliError:
        pass
    found = find_moved_image(uuid, location, search_folders(args, location), known, size,
                             recursive=not args.no_recurse)
    for c in found:
        out.write(f"{c.match}\t{c.path}" + (f"\t{c.note}" if c.note else "") + "\n")
    return 0 if any(c.match == MATCH_UUID for c in found) else 1


def cmd_chain(args, vbm, out) -> int:
    media = load_media(vbm)
    disks = media["disk"]
    if os.path.isfile(args.medium) and not any(
            same_file(r.location, args.medium) for r in disks if r.location):
        uuid, path, start = "", args.medium, args.medium
    else:
        _kind, rec = resolve_medium(media, args.medium)
        uuid, path, start = rec.uuid, "", rec.location
    known = [r.location for kind in KINDS for r in media[kind]]
    links = trace_chain(uuid, path, disks, search_folders(args, start), known)
    for depth, link in enumerate(links):
        out.write(f"{'  ' * depth}{link.name}  {link.uuid}  [{STATUS_TEXT[link.status]}]"
                  + (f"  {link.path}" if link.path else "")
                  + (f"  ({link.note})" if link.note else "") + "\n")
    if not chain_is_repairable(links):
        out.write("# the chain cannot be completed: a member was not found\n")
        return 1
    commands, register = chain_fixes(links)
    if not args.fix:
        return 0
    if not args.run:
        out.write(plan_script(commands) if commands else "")
        for path in parent_first(register):
            out.write(f"# add to library: {path}\n")
        return 0
    code = run_commands(vbm, commands, out)
    if code:
        return code
    for path in parent_first(register):
        add_library_path("disk", path)
        out.write(f"[library] added disk {path}\n")
    return 0


def cmd_history(args, _vbm, out) -> int:
    entries = load_history()
    if args.medium:
        token = args.medium
        rec = MediumRecord(uuid=token if is_uuid(token) else "",
                           location="" if is_uuid(token) else os.path.abspath(token),
                           fmt="", capacity="", size_on_disk="")
        entries = history_for(entries, rec)
    if args.json:
        import json
        out.write(json.dumps(entries, indent=1) + "\n")
    else:
        for e in entries:
            mark = "ok" if e.get("exit") == 0 else f"exit {e.get('exit')}"
            out.write(f"{e.get('when', '')}\t[{mark}]\t{e.get('command', '')}\n")
    return 0


def cmd_fix_discard(args, vbm, out) -> int:
    media = load_media(vbm)
    code, output = capture(vbm, ["list", "vms"])
    if code:
        raise CliError(f"`VBoxManage list vms` failed (exit {code}): {output.strip()}")
    infos = {}
    for _name, uuid in parse_vms_list(output):
        code, text = capture(vbm, ["showvminfo", uuid, "--machinereadable"])
        if code == 0:
            infos[uuid] = parse_machinereadable(text)
    commands, skipped = discard_fix_plan(media, infos, nonrotational=not args.no_ssd)
    for reason in skipped:
        out.write(f"# skipped: {reason}\n")
    if not args.run:
        out.write(plan_script(commands) if commands else "# nothing to change\n")
        return 0
    return run_commands(vbm, commands, out)


def cmd_backup(args, vbm, out) -> int:
    media = load_media(vbm)
    kind, rec = resolve_medium(media, args.medium)
    try:
        members = bundles.plan_members(media, kind, rec.uuid)
    except bundles.BundleError as e:
        raise CliError(str(e)) from e
    code, output = capture(vbm, ["list", "runningvms"])
    running = bundles.running_users(members, [u for _n, u in parse_vms_list(output)]) \
        if code == 0 else []
    if running and not args.allow_running:
        raise CliError("attached to a running VM: " + ", ".join(running)
                       + " (power it off, or pass --allow-running)")
    if not os.path.isdir(args.to):
        raise CliError(f"{args.to} is not a folder")
    problem = space_warning(os.path.join(args.to, "x"), bundles.bundle_size(members))
    if problem and not args.force:
        raise CliError(problem + " (pass --force to try anyway)")
    try:
        path = bundles.write_bundle(members, args.to, args.name or "")
    except (bundles.BundleError, OSError) as e:
        raise CliError(str(e)) from e
    out.write(f"{path}\n")
    return 0


def cmd_restore(args, vbm, out) -> int:
    media = load_media(vbm)
    registered = {r.uuid.lower(): r.location for kind in KINDS for r in media[kind]}
    try:
        steps, blockers = bundles.plan_restore(args.bundle, args.into, registered)
    except bundles.BundleError as e:
        raise CliError(str(e)) from e
    if blockers:
        raise CliError("; ".join(blockers))
    for step in steps:
        out.write(f"{step.name}: " + (f"skipped, {step.skip}" if step.skip
                                       else f"-> {step.target}") + "\n")
    try:
        bundles.restore_files(args.bundle, steps)
    except (bundles.BundleError, OSError) as e:
        raise CliError(str(e)) from e
    restored = [(s.kind, s.target) for s in steps if not s.skip]
    disks = parent_first([p for k, p in restored if k == "disk"])
    for kind, path in [("disk", p) for p in disks] + [(k, p) for k, p in restored
                                                      if k != "disk"]:
        add_library_path(kind, path)
        out.write(f"[library] added {kind} {path}\n")
    load_media(vbm, log=lambda line: out.write(line + "\n"))
    return 0


def cmd_verify_bundle(args, _vbm, out) -> int:
    try:
        problems = bundles.verify_bundle(args.bundle)
    except bundles.BundleError as e:
        raise CliError(str(e)) from e
    out.write("\n".join(problems) + "\n" if problems else "intact\n")
    return 1 if problems else 0


def cmd_settings(args, _vbm, out) -> int:
    if args.action == "export":
        sections = list(settings_io.DEFAULT_SECTIONS)
        if args.with_history:
            sections.append(settings_io.SECTION_HISTORY)
        try:
            settings_io.write_settings_file(args.file, sections)
        except OSError as e:
            raise CliError(f"{args.file}: {e.strerror or e}") from e
        out.write(f"exported {', '.join(sections)} to {args.file}\n")
        return 0
    try:
        data = settings_io.read_settings_file(args.file)
    except settings_io.SettingsFileError as e:
        raise CliError(str(e)) from e
    only = args.only.split(",") if args.only else None
    report = settings_io.import_settings(data, only, replace=args.replace)
    out.write(report.text() + "\n")
    return 0


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
    p.add_argument("--fix-moved", action="store_true",
                   help="find missing files by UUID and print the re-point commands")
    p.add_argument("--run", action="store_true", help="with --fix-moved: run them")
    p.add_argument("--in", dest="folders", action="append", metavar="DIR",
                   help="with --fix-moved: folder to search (repeatable)")
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
    p = sub.add_parser("locate", help="find a moved image by the UUID in its header; "
                                      "exit 1 when none matches")
    p.add_argument("medium", help="UUID, or an image file whose UUID to look for")
    p.add_argument("--in", dest="folders", action="append", metavar="DIR",
                   help="folder to search (repeatable; default: near the old location)")
    p.add_argument("--no-recurse", action="store_true")
    p = sub.add_parser("chain", help="trace a differencing chain through its files")
    p.add_argument("medium", help="registered medium (UUID or path), or an image file")
    p.add_argument("--in", dest="folders", action="append", metavar="DIR")
    p.add_argument("--fix", action="store_true", help="print the repair plan")
    p.add_argument("--run", action="store_true", help="with --fix: carry it out")
    p = sub.add_parser("history", help="commands VBoxFront has run, optionally for one "
                                       "medium")
    p.add_argument("medium", nargs="?", help="UUID or path")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("fix-discard", help="turn TRIM pass-through on for attached disks "
                                           "(prints the plan unless --run)")
    p.add_argument("--no-ssd", action="store_true",
                   help="do not also pass --nonrotational on")
    p.add_argument("--run", action="store_true")
    p = sub.add_parser("backup", help="copy a medium and its chain into a bundle")
    p.add_argument("medium", help="registered medium (UUID or path)")
    p.add_argument("--to", required=True, metavar="DIR")
    p.add_argument("--name", help="bundle folder name")
    p.add_argument("--allow-running", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore the free-space check")
    p = sub.add_parser("restore", help="restore a bundle into a folder and register it")
    p.add_argument("bundle")
    p.add_argument("--into", required=True, metavar="DIR")
    p = sub.add_parser("verify-bundle", help="re-hash a bundle; exit 1 when damaged")
    p.add_argument("bundle")
    p = sub.add_parser("settings", help="export or import VBoxFront's settings")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("file")
    p.add_argument("--with-history", action="store_true", help="export: include history")
    p.add_argument("--replace", action="store_true",
                   help="import: replace library/filters/folders instead of merging")
    p.add_argument("--only", metavar="SECTIONS",
                   help="import: comma-separated, from " + ",".join(settings_io.SECTIONS))
    return parser


HANDLERS = {"list": cmd_list, "health": cmd_health, "space": cmd_space,
            "compact-all": cmd_compact_all, "verify": cmd_verify,
            "locate": cmd_locate, "chain": cmd_chain, "history": cmd_history,
            "fix-discard": cmd_fix_discard, "backup": cmd_backup,
            "restore": cmd_restore, "verify-bundle": cmd_verify_bundle,
            "settings": cmd_settings}


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
        vbm = None if args.command in OFFLINE else require_vbm()
        return HANDLERS[args.command](args, vbm, out)
    except CliError as e:
        print(f"vboxfront: {e}", file=sys.stderr)
        return 3
