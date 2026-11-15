"""The build lab: does a package with no wheel for a platform build from its source there, and does the result work?

    python3 scripts/build_lab.py plan data/coverage.json data/builds.json [--max N] [--github-output FILE]
    python scripts/build_lab.py run --name N --version V --target T --runner R --out result.json
    python3 scripts/build_lab.py merge data/builds.json results/ plan.json

.github/workflows/build-lab.yml runs the three steps:

- plan (our code only) picks the most-downloaded packages whose current release in data/coverage.json has an sdist
  but no wheel for a lab target, skipping pairs built recently, and prints the build matrix.
- run, in a job that runs the package's own build code, builds only that package from its PyPI sdist the way pip
  does when there's no wheel (`pip wheel --no-binary NAME`: build dependencies still come as wheels), then installs
  the result and imports it, and on a free-threaded Python checks whether importing it turned the GIL back on.
  It writes one result.json. Nothing it builds is kept or published.
- merge (our code only, the one job that can write) accepts a result only if it is exactly what run writes, for
  exactly the pair the plan assigned to that job: a build can change anything in its own job, including the file.
  Then it updates data/builds.json.

Standard library only, so plan and merge never import third-party code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

SCHEMA = 1
# Each lab target: the tracker's target label, the GitHub runner it builds on and the Python it builds for.
# A docker image means the build runs in that container on the runner (Alpine: with the C toolchain installed).
TARGETS: dict[str, dict[str, str]] = {
    "3.14 win-arm64": {"runner": "windows-11-arm", "python": "3.14"},
    "3.14t win-arm64": {"runner": "windows-11-arm", "python": "3.14t"},
    "3.14t linux-x86_64": {"runner": "ubuntu-24.04", "python": "3.14t"},
    "3.14 musl-aarch64": {"runner": "ubuntu-24.04-arm", "python": "3.14", "image": "python:3.14-alpine"},
}
REFRESH_DAYS = 90  # a result for the same release is rebuilt after this long (toolchains and runners change)
DEFAULT_MAX = 40
BUILD_TIMEOUT = 40 * 60
INSTALL_TIMEOUT = 15 * 60
IMPORT_TIMEOUT = 5 * 60
MAX_EXCERPT = 400
MAX_RESULT_BYTES = 20_000
MAX_BUILDS_BYTES = 5_000_000

OUTCOMES = {"built", "failed", "timeout"}
IMPORTS = {"ok", "failed", "deps-failed"}  # or None when nothing was built
GIL = {"kept-off", "re-enabled"}  # or None off free-threaded Pythons, or when the import failed
# Why a build failed, from its log; the first that matches wins. "compile error" and "other" are the fallbacks.
REASONS: list[tuple[str, re.Pattern[str]]] = [
    ("needs OpenSSL", re.compile(r"openssl", re.IGNORECASE)),
    (
        "needs a Rust toolchain",
        re.compile(
            r"\b(cargo|rustc)\b[^\n]{0,60}(is not installed|not found|could not be found)|install rust", re.IGNORECASE
        ),
    ),
    (
        "needs a C/C++ compiler",
        re.compile(
            r"Microsoft Visual C\+\+ \d|(gcc|cc|cl\.exe|g\+\+)\W+(command )?not found|unable to find vcvarsall",
            re.IGNORECASE,
        ),
    ),
    ("needs CMake", re.compile(r"cmake\W+(command )?not found|could not find cmake|no cmake", re.IGNORECASE)),
    (
        "needs a system library",
        re.compile(r"fatal error: [\w/.+-]+\.h: No such file|cannot find -l\w+|pkg-config|pg_config", re.IGNORECASE),
    ),
    (
        "build dependency failed",
        re.compile(
            r"Failed to build installable wheels for some pyproject.toml based projects|installing build dependencies did not run successfully",
            re.IGNORECASE,
        ),
    ),
    (
        "unsupported platform",
        re.compile(r"not supported on|unsupported (platform|architecture)|does not support", re.IGNORECASE),
    ),
    ("compile error", re.compile(r"\berror\b", re.IGNORECASE)),
]
OTHER = "other"
REASON_NAMES = {name for name, _ in REASONS} | {OTHER}
TOOLCHAINS = ("rustc", "cargo", "gcc", "cl", "cmake")
NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
VERSION = re.compile(r"^[0-9][0-9A-Za-z.+!_-]{0,39}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESULT_KEYS = {
    "schema",
    "name",
    "version",
    "target",
    "runner",
    "python",
    "outcome",
    "seconds",
    "import",
    "gil",
    "reason",
    "excerpt",
    "toolchains",
    "date",
}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def today() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


# --- plan ---------------------------------------------------------------------------------------------------------


def plan(coverage: dict, builds: dict | None, limit: int, now: str) -> list[dict]:
    """The pairs to build, most-downloaded first and taking turns between the lab targets, at most `limit`."""
    labels = [t["label"] for t in coverage["targets"]]
    recent = set()
    cutoff = dt.date.fromisoformat(now) - dt.timedelta(days=REFRESH_DAYS)
    for r in (builds or {}).get("results", []):
        if dt.date.fromisoformat(r["date"]) >= cutoff:
            recent.add((canonical(r["name"]), r["version"], r["target"]))
    queues: dict[str, list[dict]] = {t: [] for t in TARGETS}
    for p in sorted(coverage["packages"], key=lambda p: p["rank"]):
        name, version = p["name"], p["version"]
        if p.get("error") or not isinstance(name, str) or not NAME.match(name) or not VERSION.match(str(version)):
            continue
        for target in TARGETS:
            if target not in labels or p["results"][labels.index(target)] != "source":
                continue
            if (canonical(name), version, target) in recent:
                continue
            queues[target].append({"name": name, "version": version, "target": target, "rank": p["rank"]})
    picked: list[dict] = []
    while len(picked) < limit and any(queues.values()):
        for target in TARGETS:
            if queues[target] and len(picked) < limit:
                picked.append(queues[target].pop(0))
    for i, job in enumerate(picked):
        job.update(id=str(i), runner=TARGETS[job["target"]]["runner"], python=TARGETS[job["target"]]["python"])
        if "image" in TARGETS[job["target"]]:
            job["image"] = TARGETS[job["target"]]["image"]
    return picked


def cmd_plan(args: argparse.Namespace) -> int:
    coverage = json.loads(Path(args.coverage).read_text(encoding="utf-8"))
    builds_path = Path(args.builds)
    builds = json.loads(builds_path.read_text(encoding="utf-8")) if builds_path.exists() else None
    jobs = plan(coverage, builds, args.max, today())
    hosted = [j for j in jobs if "image" not in j]
    docker = [j for j in jobs if "image" in j]
    print(
        f"{len(jobs)} builds: " + ", ".join(f"{j['name']} {j['version']} ({j['target']})" for j in jobs),
        file=sys.stderr,
    )
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"count={len(jobs)}\n")
            f.write(f"hosted={json.dumps(hosted)}\nhosted_count={len(hosted)}\n")
            f.write(f"docker={json.dumps(docker)}\ndocker_count={len(docker)}\n")
            f.write(f"all={json.dumps(jobs)}\n")
    else:
        print(json.dumps(jobs, indent=1))
    return 0


# --- run (inside the build job) -----------------------------------------------------------------------------------


def classify(log: str) -> str:
    for name, pattern in REASONS:
        if pattern.search(log):
            return name
    return OTHER


def excerpt(log: str) -> str:
    """The last lines that say what went wrong, as printable ASCII: it ends up on a web page and in terminals."""
    lines = [l.strip() for l in log.splitlines() if re.search(r"error|fatal|not found|failed", l, re.IGNORECASE)]
    text = " | ".join(lines[-4:]) or " | ".join(l.strip() for l in log.splitlines()[-3:])
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = "".join(c if 32 <= ord(c) < 127 else "?" for c in text)
    return text[-MAX_EXCERPT:]


def top_level_modules(wheel: Path) -> list[str]:
    """The importable top-level names in a wheel: packages (with __init__.py), modules and extension modules."""
    names: dict[str, None] = {}
    with zipfile.ZipFile(wheel) as z:
        entries = z.namelist()
    for entry in entries:
        first, _, rest = entry.partition("/")
        if first.endswith((".dist-info", ".data")):
            continue
        if rest == "__init__.py":
            names[first] = None
        elif not rest and first.endswith(".py"):
            names[first[:-3]] = None
        elif not rest and first.endswith((".so", ".pyd")):
            names[first.split(".", 1)[0]] = None
    public = [n for n in names if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) and not n.startswith("_")]
    return (public or [n for n in names if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)])[:3]


IMPORT_CHECK = """
import importlib, json, sys, warnings
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    for m in sys.argv[1:]:
        importlib.import_module(m)
gil = getattr(sys, "_is_gil_enabled", None)
print("WHEELGAP-LAB " + json.dumps({"gil_enabled": gil() if gil else None}))
"""


def run_build(name: str, version: str, free_threaded: bool, work: Path) -> dict:
    """Build, install and import; the result fields that depend on what happened."""
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    wheels = work / "wheels"
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-binary",
        name,
        "--no-cache-dir",
        "-w",
        str(wheels),
        f"{name}=={version}",
    ]
    start = time.monotonic()
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=BUILD_TIMEOUT, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        return {
            "outcome": "timeout",
            "seconds": BUILD_TIMEOUT,
            "import": None,
            "gil": None,
            "reason": None,
            "excerpt": "",
        }
    seconds = round(time.monotonic() - start)
    log = p.stdout + p.stderr
    print(log[-6000:])
    built = sorted(wheels.glob("*.whl"))
    if p.returncode != 0 or not built:
        return {
            "outcome": "failed",
            "seconds": seconds,
            "import": None,
            "gil": None,
            "reason": classify(log),
            "excerpt": excerpt(log),
        }
    result = {"outcome": "built", "seconds": seconds, "import": None, "gil": None, "reason": None, "excerpt": ""}
    install = [sys.executable, "-m", "pip", "install", "--no-cache-dir", str(built[0])]
    try:
        i = subprocess.run(
            install, capture_output=True, text=True, errors="replace", timeout=INSTALL_TIMEOUT, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        i = None
    if i is None or i.returncode != 0:
        print((i.stdout + i.stderr)[-3000:] if i else "dependency install timed out")
        result["import"] = "deps-failed"
        return result
    modules = top_level_modules(built[0])
    try:
        c = subprocess.run(
            [sys.executable, "-c", IMPORT_CHECK, *modules],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=IMPORT_TIMEOUT,
            cwd=work,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        c = None
    line = next((l for l in reversed((c.stdout if c else "").splitlines()) if l.startswith("WHEELGAP-LAB ")), None)
    if c is None or c.returncode != 0 or line is None:
        print((c.stdout + c.stderr)[-3000:] if c else "import timed out")
        result["import"] = "failed"
        result["excerpt"] = excerpt(c.stderr) if c else "import timed out"
        return result
    result["import"] = "ok"
    if free_threaded:
        result["gil"] = "re-enabled" if json.loads(line.removeprefix("WHEELGAP-LAB "))["gil_enabled"] else "kept-off"
    return result


def cmd_run(args: argparse.Namespace) -> int:
    target = args.target
    if target not in TARGETS or not NAME.match(args.name) or not VERSION.match(args.version):
        print(f"build_lab: not a lab job: {args.name} {args.version} {target}", file=sys.stderr)
        return 2
    free_threaded = TARGETS[target]["python"].endswith("t")
    with tempfile.TemporaryDirectory() as tmp:
        fields = run_build(args.name, args.version, free_threaded, Path(tmp))
    result = {
        "schema": SCHEMA,
        "name": args.name,
        "version": args.version,
        "target": target,
        "runner": args.runner,
        "python": ".".join(map(str, sys.version_info[:3])) + ("t" if free_threaded else ""),
        **fields,
        "toolchains": [t for t in TOOLCHAINS if shutil.which(t)],
        "date": today(),
    }
    Path(args.out).write_text(json.dumps(result) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


# --- merge --------------------------------------------------------------------------------------------------------


def problems(result: object, job: dict) -> list[str]:
    """Why `result` isn't exactly what run writes for this plan job (empty if it is)."""
    if not isinstance(result, dict):
        return ["not an object"]
    out = []
    if set(result) != RESULT_KEYS:
        out.append(f"keys {sorted(set(result) ^ RESULT_KEYS)}")
        return out
    for key in ("name", "version", "target", "runner"):
        if result[key] != job[key]:
            out.append(f"{key} {result[key]!r} isn't the planned {job[key]!r}")
    if result["schema"] != SCHEMA:
        out.append("schema")
    if not (isinstance(result["python"], str) and re.fullmatch(r"3\.\d{1,2}\.\d{1,3}t?", result["python"])):
        out.append("python")
    elif result["python"].endswith("t") != job["python"].endswith("t") or not result["python"].startswith(
        job["python"].rstrip("t") + "."
    ):
        out.append("python isn't the planned one")
    if result["outcome"] not in OUTCOMES:
        out.append("outcome")
    seconds = result["seconds"]
    if not (isinstance(seconds, int) and not isinstance(seconds, bool) and 0 <= seconds <= BUILD_TIMEOUT):
        out.append("seconds")
    built = result["outcome"] == "built"
    if (result["import"] in IMPORTS) != built or (result["import"] is not None and not built):
        out.append("import")
    # The GIL check is made exactly when a free-threaded build imported.
    gil_expected = result["import"] == "ok" and job["python"].endswith("t")
    if (result["gil"] in GIL) != gil_expected or (result["gil"] is not None and result["gil"] not in GIL):
        out.append("gil")
    if (result["reason"] is None) != (result["outcome"] != "failed") or (
        result["reason"] is not None and result["reason"] not in REASON_NAMES
    ):
        out.append("reason")
    text = result["excerpt"]
    if not (isinstance(text, str) and len(text) <= MAX_EXCERPT and all(32 <= ord(c) < 127 for c in text)):
        out.append("excerpt")
    tools = result["toolchains"]
    if not (isinstance(tools, list) and all(t in TOOLCHAINS for t in tools) and len(set(tools)) == len(tools)):
        out.append("toolchains")
    if not (isinstance(result["date"], str) and DATE.match(result["date"])):
        out.append("date")
    return out


def merge(builds: dict | None, results: dict[str, object], jobs: list[dict], now: str) -> tuple[dict, list[str]]:
    """data/builds.json with the valid results in: one entry per (package, target), the newest release's."""
    notes = []
    current = {(canonical(r["name"]), r["target"]): r for r in (builds or {}).get("results", [])}
    for job in jobs:
        result = results.get(job["id"])
        if result is None:
            notes.append(f"{job['name']} {job['version']} {job['target']}: no result (the job failed or was cancelled)")
            continue
        bad = problems(result, job)
        if bad:
            notes.append(f"{job['name']} {job['version']} {job['target']}: rejected ({'; '.join(bad)})")
            continue
        current[(canonical(job["name"]), job["target"])] = result
    ordered = sorted(current.values(), key=lambda r: (canonical(r["name"]), r["target"]))
    return {"schema": SCHEMA, "updated": now, "targets": TARGETS, "results": ordered}, notes


def cmd_merge(args: argparse.Namespace) -> int:
    builds_path, results_dir = Path(args.builds), Path(args.results)
    builds = json.loads(builds_path.read_text(encoding="utf-8")) if builds_path.exists() else None
    jobs = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    results: dict[str, object] = {}
    for job in jobs:
        f = results_dir / f"result-{job['id']}" / "result.json"
        if f.is_file() and not f.is_symlink() and f.stat().st_size <= MAX_RESULT_BYTES:
            try:
                results[job["id"]] = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                results[job["id"]] = "unreadable"
    merged, notes = merge(builds, results, jobs, today())
    for note in notes:
        print(f"::warning::build_lab: {note}")
    text = json.dumps(merged, indent=1) + "\n"
    if len(text.encode()) > MAX_BUILDS_BYTES:
        print("::error::build_lab: builds.json would be too big; not written")
        return 1
    builds_path.write_text(text, encoding="utf-8")
    kept = sum(1 for j in jobs if j["id"] in results and not problems(results[j["id"]], j))
    print(f"build_lab: {kept} of {len(jobs)} results merged")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("coverage")
    p.add_argument("builds")
    p.add_argument("--max", type=int, default=DEFAULT_MAX)
    p.add_argument("--github-output")
    p.set_defaults(func=cmd_plan)
    r = sub.add_parser("run")
    for flag in ("--name", "--version", "--target", "--runner", "--out"):
        r.add_argument(flag, required=True)
    r.set_defaults(func=cmd_run)
    m = sub.add_parser("merge")
    m.add_argument("builds")
    m.add_argument("results")
    m.add_argument("plan")
    m.set_defaults(func=cmd_merge)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
