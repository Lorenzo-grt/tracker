"""Offline tests for scripts/gap_wheels.py: the plan, the merge job's checks on untrusted results, and the verdicts."""

import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import gap_wheels


def row(name, version, target, **over):
    r = {"name": name, "version": version, "target": target, "outcome": "built", "import": "ok", "extensions": 2,
         "gil": "kept-off" if target.split()[0].endswith("t") else None}  # fmt: skip
    r.update(over)
    return r


BUILDS = {
    "results": [
        row("tiktoken", "0.14.0", "3.14 win-arm64"),
        row("lz4", "4.4.5", "3.14t win-arm64"),
        row("zstandard", "0.25.0", "3.14 musl-aarch64"),
        row("broken", "1.0", "3.14 win-arm64", outcome="failed"),
        row("noimport", "1.0", "3.14 win-arm64", **{"import": "failed"}),
        row("pureish", "1.0", "3.14 win-arm64", extensions=0),  # nothing compiled: PyPI's pure wheel would do
        row("gilon", "1.0", "3.14t win-arm64", gil="re-enabled"),
        row("weird", "1.0; rm -rf /", "3.14 win-arm64"),
        row("elsewhere", "1.0", "3.14 linux-x86_64"),  # not a lab target
    ]
}


def good(job, **over):
    platform = {"3.14 win-arm64": "cp314-cp314-win_arm64", "3.14t win-arm64": "cp314-cp314t-win_arm64",
                "3.14 musl-aarch64": "cp314-cp314-musllinux_1_2_aarch64"}[job["target"]]  # fmt: skip
    r = {
        "schema": 1,
        "name": job["name"],
        "version": job["version"],
        "target": job["target"],
        "attempt": job["attempt"],
        "runner": job["runner"],
        "sdist": f"{job['name']}-{job['version']}.tar.gz",
        "sdist_sha256": "a" * 64,
        "wheel": f"{job['name']}-{job['version']}-{platform}.whl",
        "sha256": "b" * 64,
        "installs": True if job["attempt"] == 1 else None,
    }
    r.update(over)
    return r


class Plan(unittest.TestCase):
    def test_only_clean_builds_on_lab_targets(self):
        pairs = {(p["name"], p["target"]) for p in gap_wheels.clean_pairs(BUILDS)}
        self.assertEqual(pairs, {("tiktoken", "3.14 win-arm64"), ("lz4", "3.14t win-arm64"),
                                 ("zstandard", "3.14 musl-aarch64")})  # fmt: skip

    def test_two_builds_per_pair_and_docker_for_alpine(self):
        jobs = gap_wheels.plan(BUILDS, 10)
        self.assertEqual(len(jobs), 6)
        self.assertEqual({(j["id"], j["attempt"]) for j in jobs}, {(i, a) for i in "012" for a in (1, 2)})
        alpine = [j for j in jobs if j["target"] == "3.14 musl-aarch64"]
        self.assertTrue(alpine and all(j["image"].startswith("python:3.14-alpine@sha256:") for j in alpine))

    def test_max_and_only(self):
        self.assertEqual(len(gap_wheels.plan(BUILDS, 1)), 2)
        self.assertEqual({j["name"] for j in gap_wheels.plan(BUILDS, 10, {"lz4"})}, {"lz4"})


class Verify(unittest.TestCase):
    def setUp(self):
        self.jobs = gap_wheels.plan(BUILDS, 1, {"tiktoken"})
        self.one, self.two = self.jobs

    def verdict(self, first, second):
        results = {}
        if first is not None:
            results[("0", 1)] = first
        if second is not None:
            results[("0", 2)] = second
        verified, report = gap_wheels.verify(self.jobs, results)
        return verified, report[0]["verdict"]

    def test_identical_and_installs(self):
        verified, verdict = self.verdict(good(self.one), good(self.two))
        self.assertEqual(verdict, "verified")
        self.assertEqual(verified[0]["wheel"], "tiktoken-0.14.0-cp314-cp314-win_arm64.whl")
        self.assertEqual(verified[0]["sha256"], "b" * 64)

    def test_builds_differ(self):
        self.assertEqual(self.verdict(good(self.one), good(self.two, sha256="c" * 64))[1], "the two builds differ")

    def test_different_sdists(self):
        self.assertEqual(self.verdict(good(self.one), good(self.two, sdist_sha256="c" * 64))[1],
                         "the two builds differ")  # fmt: skip

    def test_doesnt_install(self):
        self.assertEqual(self.verdict(good(self.one, installs=False), good(self.two))[1],
                         "pip couldn't install it without compiling")  # fmt: skip

    def test_missing_result(self):
        self.assertEqual(self.verdict(good(self.one), None)[1], "a build has no result")

    def test_no_wheel(self):
        self.assertEqual(self.verdict(good(self.one), good(self.two, wheel=None, sha256=None))[1], "a build made no wheel")

    def test_a_result_for_another_job_is_rejected(self):
        for over in ({"name": "numpy"}, {"version": "0.13.0"}, {"target": "3.14t win-arm64"}, {"attempt": 1},
                     {"runner": "ubuntu-24.04"}):  # fmt: skip
            with self.subTest(over):
                verified, verdict = self.verdict(good(self.one), good(self.two, **over))
                self.assertTrue(verdict.startswith("rejected"), verdict)
                self.assertEqual(verified, [])

    def test_a_wheel_for_another_project_version_or_platform_is_rejected(self):
        for wheel in ("numpy-0.14.0-cp314-cp314-win_arm64.whl", "tiktoken-0.13.0-cp314-cp314-win_arm64.whl",
                      "tiktoken-0.14.0-cp314-cp314-win_amd64.whl", "tiktoken-0.14.0-py3-none-any.whl",
                      "../evil.whl", "tiktoken.whl"):  # fmt: skip
            with self.subTest(wheel):
                verdict = self.verdict(good(self.one, wheel=wheel), good(self.two, wheel=wheel))[1]
                self.assertTrue(verdict.startswith("rejected"), verdict)

    def test_malformed_results_are_rejected(self):
        for bad in ("not JSON", [], {**good(self.two), "extra": 1}, good(self.two, sha256="B" * 64),
                    good(self.two, installs="yes"), good(self.two, installs=True), good(self.two, schema=2),
                    good(self.two, sdist="x;y.tar.gz"), good(self.two, sha256=None)):  # fmt: skip
            with self.subTest(bad):
                self.assertTrue(self.verdict(good(self.one), bad)[1].startswith("rejected"))


class Merge(unittest.TestCase):
    def test_writes_manifest_and_summary(self):
        jobs = gap_wheels.plan(BUILDS, 2)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "plan.json").write_text(json.dumps(jobs))
            for job in jobs:
                d = tmp / "results" / f"result-{job['id']}-{job['attempt']}"
                d.mkdir(parents=True)
                sha = "c" * 64 if job["id"] == "1" and job["attempt"] == 2 else "b" * 64
                (d / "result.json").write_text(json.dumps(good(job, sha256=sha)))
            big = tmp / "results" / "result-9-1"
            big.mkdir()
            (big / "result.json").write_text("x" * 20_000)  # not in the plan: never read
            gap_wheels.main(["merge", str(tmp / "results"), "--plan", str(tmp / "plan.json"), "--out",
                             str(tmp / "manifest.json"), "--summary", str(tmp / "summary.md")])  # fmt: skip
            manifest = json.loads((tmp / "manifest.json").read_text())
            summary = (tmp / "summary.md").read_text()
        self.assertEqual([w["name"] for w in manifest["wheels"]], ["lz4"])
        self.assertIn("**1 verified** of 2 pairs", summary)
        self.assertIn("the two builds differ", summary)

    def test_an_oversized_result_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text(" " * 20_000 + "{}")
            self.assertEqual(gap_wheels.read_result(path), "too big")


class Unpack(unittest.TestCase):
    def test_refuses_paths_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sdist = tmp / "evil-1.0.tar.gz"
            with tarfile.open(sdist, "w:gz") as t:
                info = tarfile.TarInfo("../outside.txt")
                info.size = 1
                t.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(tarfile.TarError):
                gap_wheels.unpack(sdist, tmp / "dest")
            self.assertFalse((tmp / "outside.txt").exists())
            z = tmp / "evil-1.0.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("../outside.txt", "x")
            with self.assertRaises(ValueError):
                gap_wheels.unpack(z, tmp / "dest")

    def test_one_top_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            z = tmp / "ok-1.0.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("ok-1.0/setup.py", "")
            self.assertEqual(gap_wheels.unpack(z, tmp / "dest").name, "ok-1.0")


if __name__ == "__main__":
    unittest.main()
