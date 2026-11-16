"""Gap wheels, step 1: verify which of the build lab's clean builds are reproducible and install. Nothing is published.

    python3 scripts/gap_wheels.py plan data/builds.json [--max N] [--only a,b] --github-output FILE
    python scripts/gap_wheels.py build --name NAME --version VERSION --target TARGET --attempt 1|2 --runner R --out FILE
    python3 scripts/gap_wheels.py merge RESULTS_DIR --plan PLAN_JSON --out MANIFEST [--summary FILE]

A *pair* is a release and lab target the build lab built cleanly (data/builds.json: built, imported, a compiled
extension module, and on free-threaded Python the GIL kept off), so PyPI has no wheel for it there. Each pair is built
twice, in separate jobs on separate runners:

- from the sdist alone (`pip download --no-binary NAME`: build tools still come as wheels, as pip installs them),
- unpacked into a fixed folder, so the build path baked into a binary is the same both times,
- with SOURCE_DATE_EPOCH fixed, and on Windows with `-Brepro` for MSVC's compiler and linker and `codegen-units=1`
  for Rust (spelt `-Brepro`, since Git Bash rewrites a leading `/` into a Windows path).

A wheel is *verified* when both builds give byte-identical wheels and pip installs it, with `--only-binary :all:`
(compiling nothing), into a fresh virtual environment on the first build's runner. The merge job writes a manifest
of the verified wheels: filename, sha256, the sdist's name and sha256, and the run that built them.

Build jobs run each package's own build code with the runner's admin rights, so they're untrusted, as in the build
lab: they get a read-only token and no secrets, and merge accepts a result only if it is exactly what `build` writes,
for the pair and attempt the plan gave that job. Requiring two builds to match is what makes a wheel worth trusting:
a build that tampered with its own output, or another job's, would have to do it identically in both. This step
uploads no wheel anywhere: only results (hashes) leave the build jobs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_lab  # TARGETS, interpreter_problem, the name and version rules

SCHEMA = 1
SDIST = re.compile(r"[A-Za-z0-9._-]{1,120}\.(?:tar\.gz|zip)", re.ASCII)
WHEEL = re.compile(r"[A-Za-z0-9._+!-]{1,200}\.whl", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
RESULT_KEYS = frozenset(
    {"schema", "name", "version", "target", "attempt", "runner", "sdist", "sdist_sha256", "wheel", "sha256", "installs"}
)
MAX_RESULT_BYTES = 10_000
BUILD_TIMEOUT = build_lab.BUILD_TIMEOUT
INSTALL_TIMEOUT = build_lab.INSTALL_TIMEOUT


def clean_pairs(builds: dict) -> list[dict]:
    """The lab's clean builds: (name, version, target) with a compiled module that imported, GIL off where it applies."""
    out = []
    for r in builds.get("results", []):
        if (
            r.get("outcome") == "built"
            and r.get("import") == "ok"
            and isinstance(r.get("extensions"), int)
            and r["extensions"] > 0
            and r.get("gil") != "re-enabled"
            and r.get("target") in build_lab.TARGETS
            and build_lab.NAME.fullmatch(str(r.get("name")))
            and build_lab.VERSION.fullmatch(str(r.get("version")))
        ):
            out.append({"name": r["name"], "version": r["version"], "target": r["target"]})
    return sorted(out, key=lambda p: (build_lab.canonical(p["name"]), p["target"]))


def plan(builds: dict, limit: int, only: set[str] | None = None) -> list[dict]:
    """Two build jobs per pair, at most `limit` pairs, each with the runner (and Docker image) of its target."""
    jobs = []
    pairs = [p for p in clean_pairs(builds) if only is None or build_lab.canonical(p["name"]) in only][:limit]
    for i, pair in enumerate(pairs):
        spec = build_lab.TARGETS[pair["target"]]
        for attempt in (1, 2):
            job = {**pair, "id": str(i), "attempt": attempt, "runner": spec["runner"], "python": spec["python"]}
            if "image" in spec:
                job["image"] = spec["image"]
            jobs.append(job)
    return jobs


def cmd_plan(args: argparse.Namespace) -> int:
    builds = json.loads(Path(args.builds).read_text(encoding="utf-8"))
    only = {build_lab.canonical(n.strip()) for n in args.only.split(",") if n.strip()} if args.only else None
    jobs = plan(builds, args.max, only)
    hosted = [j for j in jobs if "image" not in j]
    docker = [j for j in jobs if "image" in j]
    print(f"{len(jobs) // 2} pairs, {len(jobs)} builds", file=sys.stderr)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"hosted={json.dumps(hosted)}\nhosted_count={len(hosted)}\n")
            f.write(f"docker={json.dumps(docker)}\ndocker_count={len(docker)}\n")
            f.write(f"all={json.dumps(jobs)}\n")
    else:
        print(json.dumps(jobs, indent=1))
    return 0


# --- build (inside the untrusted job) ------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unpack(sdist: Path, dest: Path) -> Path:
    """Unpack an sdist into `dest` (emptied first) and return its one top-level folder. tarfile's `data` filter refuses
    absolute paths, `..`, links out of the folder and device files; a zip member that would land outside is refused."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if sdist.name.endswith(".zip"):
        with zipfile.ZipFile(sdist) as z:
            for member in z.namelist():
                if not (dest / member).resolve().is_relative_to(dest.resolve()):
                    raise ValueError(f"{member}: outside the sdist's folder")
            z.extractall(dest)
    else:
        with tarfile.open(sdist) as t:
            t.extractall(dest, filter="data")
    tops = list(dest.iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise ValueError("the sdist doesn't unpack into one folder")
    return tops[0]


def build_env() -> dict[str, str]:
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", SOURCE_DATE_EPOCH="1700000000")
    if os.name == "nt":
        env.update(CL="-Brepro", LINK="-Brepro", RUSTFLAGS="-C link-arg=-Brepro -C codegen-units=1")
    return env


def run_build(name: str, version: str, attempt: int, work: Path) -> dict:
    """Download the sdist, build the wheel from a fixed folder, hash it, and (first attempt) install it from a local
    folder into a fresh venv with nothing compiled. Fields left None mean that step didn't happen."""
    fields: dict = {"sdist": None, "sdist_sha256": None, "wheel": None, "sha256": None, "installs": None}
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    sdists = work / "sdist"
    pip = [sys.executable, "-m", "pip"]
    subprocess.run([*pip, "download", "--no-deps", "--no-binary", name, "--no-cache-dir", "-d", str(sdists),
                    f"{name}=={version}"], check=False, env=env, timeout=BUILD_TIMEOUT)  # fmt: skip
    found = [p for p in sdists.glob("*") if SDIST.fullmatch(p.name)] if sdists.is_dir() else []
    if len(found) != 1:
        print(f"gap_wheels: expected one sdist, found {len(found)}", file=sys.stderr)
        return fields
    fields.update(sdist=found[0].name, sdist_sha256=sha256(found[0]))
    fixed = Path("C:/gw" if os.name == "nt" else "/tmp/gw")
    try:
        src = unpack(found[0], fixed)
    except (ValueError, tarfile.TarError, zipfile.BadZipFile, OSError) as e:
        print(f"gap_wheels: can't unpack the sdist: {e}", file=sys.stderr)
        return fields
    out = work / "out"
    try:
        p = subprocess.run([*pip, "wheel", "--no-deps", "--no-cache-dir", "-w", str(out), str(src)], check=False,
                           env=build_env(), capture_output=True, text=True, errors="replace",
                           timeout=BUILD_TIMEOUT)  # fmt: skip
        print((p.stdout + p.stderr)[-6000:])
    except subprocess.TimeoutExpired:
        print("gap_wheels: the build timed out", file=sys.stderr)
        return fields
    wheels = sorted(out.glob("*.whl")) if out.is_dir() else []
    if len(wheels) != 1 or not WHEEL.fullmatch(wheels[0].name):
        print(f"gap_wheels: expected one wheel, found {len(wheels)}", file=sys.stderr)
        return fields
    fields.update(wheel=wheels[0].name, sha256=sha256(wheels[0]))
    if attempt == 1:
        venv = work / "fresh"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=INSTALL_TIMEOUT)
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        try:
            i = subprocess.run([str(py), "-m", "pip", "install", "--only-binary", ":all:", "--no-cache-dir",
                                "--find-links", str(out), f"{name}=={version}"], check=False, env=env,
                               capture_output=True, text=True, errors="replace",
                               timeout=INSTALL_TIMEOUT)  # fmt: skip
            print((i.stdout + i.stderr)[-3000:])
            fields["installs"] = i.returncode == 0
        except subprocess.TimeoutExpired:
            fields["installs"] = False
    return fields


def cmd_build(args: argparse.Namespace) -> int:
    target = args.target
    if (
        target not in build_lab.TARGETS
        or not build_lab.NAME.fullmatch(args.name)
        or not build_lab.VERSION.fullmatch(args.version)
        or args.attempt not in (1, 2)
    ):
        print(f"gap_wheels: not a gap-wheels job: {args.name} {args.version} {target}", file=sys.stderr)
        return 2
    if (why := build_lab.interpreter_problem(target)) is not None:
        print(f"gap_wheels: this Python isn't {target}'s: {why}", file=sys.stderr)
        return 3
    with tempfile.TemporaryDirectory() as tmp:
        fields = run_build(args.name, args.version, args.attempt, Path(tmp))
    result = {"schema": SCHEMA, "name": args.name, "version": args.version, "target": target,
              "attempt": args.attempt, "runner": args.runner, **fields}  # fmt: skip
    Path(args.out).write_text(json.dumps(result) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


# --- merge (trusted) -----------------------------------------------------------------------------------------------


def wheel_problem(wheel: object, job: dict) -> str | None:
    """Why a result's wheel filename can't be this pair's (None if it can): the project, version and platform tag."""
    if not isinstance(wheel, str) or not WHEEL.fullmatch(wheel):
        return "wheel"
    parts = wheel[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        return "wheel name"
    name, version, platform = parts[0], parts[1], parts[-1]
    if build_lab.canonical(name) != build_lab.canonical(job["name"]):
        return "wheel is for another project"
    if version.replace("_", "-") != job["version"] and version != job["version"]:
        return "wheel is for another version"
    if not re.fullmatch(build_lab.TARGETS[job["target"]]["wheel"], platform, re.ASCII):
        return "wheel isn't for the target"
    return None


def problems(result: object, job: dict) -> list[str]:
    """What's wrong with an untrusted result for `job` (empty when it's exactly what `build` writes for that job)."""
    if not isinstance(result, dict):
        return ["not an object"]
    if set(result) != RESULT_KEYS:
        return ["keys"]
    out = []
    if result["schema"] != SCHEMA:
        out.append("schema")
    for key in ("name", "version", "target", "attempt", "runner"):
        if result[key] != job[key]:
            out.append(f"{key} isn't the planned one")
    if result["sdist"] is not None and not (isinstance(result["sdist"], str) and SDIST.fullmatch(result["sdist"])):
        out.append("sdist")
    for key in ("sdist_sha256", "sha256"):
        if result[key] is not None and not (isinstance(result[key], str) and SHA256.fullmatch(result[key])):
            out.append(key)
    if result["wheel"] is not None and (why := wheel_problem(result["wheel"], job)):
        out.append(why)
    if (result["wheel"] is None) != (result["sha256"] is None):
        out.append("wheel and sha256 go together")
    if result["installs"] is not None and not isinstance(result["installs"], bool):
        out.append("installs")
    if job["attempt"] == 2 and result["installs"] is not None:
        out.append("only the first build installs")
    return out


def verify(jobs: list[dict], results: dict[tuple[str, int], object]) -> tuple[list[dict], list[dict]]:
    """(verified wheels, every pair with its verdict) from the plan's jobs and the results by (id, attempt)."""
    verified, report = [], []
    by_id: dict[str, dict[int, dict]] = {}
    for job in jobs:
        by_id.setdefault(job["id"], {})[job["attempt"]] = job
    for pid, attempts in sorted(by_id.items(), key=lambda kv: int(kv[0])):
        job = attempts[1]
        row = {"name": job["name"], "version": job["version"], "target": job["target"]}
        got = {a: results.get((pid, a)) for a in (1, 2)}
        bad = [f"build {a}: {p}" for a in (1, 2) if got[a] is not None for p in problems(got[a], attempts[a])]
        first, second = got[1], got[2]
        if bad:
            verdict = "rejected: " + "; ".join(bad)
        elif first is None or second is None:
            verdict = "a build has no result"
        elif first["sha256"] is None or second["sha256"] is None:
            verdict = "a build made no wheel"
        elif (first["wheel"], first["sha256"], first["sdist_sha256"]) != (
            second["wheel"],
            second["sha256"],
            second["sdist_sha256"],
        ):
            verdict = "the two builds differ"
        elif first["installs"] is not True:
            verdict = "pip couldn't install it without compiling"
        else:
            verdict = "verified"
            verified.append({**row, "wheel": first["wheel"], "sha256": first["sha256"], "sdist": first["sdist"],
                             "sdist_sha256": first["sdist_sha256"]})  # fmt: skip
        report.append({**row, "verdict": verdict})
    return verified, report


def read_result(path: Path) -> object:
    if path.stat().st_size > MAX_RESULT_BYTES:
        return "too big"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "not JSON"


def cmd_merge(args: argparse.Namespace) -> int:
    jobs = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    results: dict[tuple[str, int], object] = {}
    for job in jobs:
        path = Path(args.results) / f"result-{job['id']}-{job['attempt']}" / "result.json"
        if path.is_file():
            results[(job["id"], job["attempt"])] = read_result(path)
    verified, report = verify(jobs, results)
    run = ""
    if all(os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")):
        run = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    date = dt.datetime.now(dt.UTC).date().isoformat()
    manifest = {"schema": SCHEMA, "date": date, "run": run, "wheels": verified}
    Path(args.out).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    head = f"**{len(verified)} verified** of {len(report)} pairs (byte-identical twice, and pip installs it with nothing compiled)"
    lines = [head, "", "| package | version | target | verdict |", "|---|---|---|---|"]
    lines += [f"| {r['name']} | {r['version']} | {r['target']} | {r['verdict']} |" for r in report]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("builds")
    p.add_argument("--max", type=int, default=100)
    p.add_argument("--only")
    p.add_argument("--github-output")
    p.set_defaults(func=cmd_plan)
    b = sub.add_parser("build")
    for flag in ("--name", "--version", "--target", "--runner", "--out"):
        b.add_argument(flag, required=True)
    b.add_argument("--attempt", type=int, required=True)
    b.set_defaults(func=cmd_build)
    m = sub.add_parser("merge")
    m.add_argument("results")
    m.add_argument("--plan", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--summary")
    m.set_defaults(func=cmd_merge)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
