"""Offline tests for scripts/build_lab.py: the plan, the merge job's checks on untrusted results, and the helpers."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_lab

TARGETS = ["3.14 win-arm64", "3.14t win-arm64", "3.14t linux-x86_64", "3.14 musl-aarch64", "3.14 linux-x86_64"]


def pkg(rank, name, version, statuses):
    return {"rank": rank, "name": name, "version": version, "pure": False, "error": None, "results": statuses}


COVERAGE = {
    "targets": [{"label": t} for t in TARGETS],
    "packages": [
        pkg(2, "tiktoken", "0.14.0", ["source", "source", "wheel", "wheel", "wheel"]),
        pkg(1, "cryptography", "50.0.2", ["source", "wheel", "wheel", "wheel", "wheel"]),
        pkg(3, "orjson", "3.13.0", ["wheel", "source", "source", "wheel", "wheel"]),
        pkg(4, "torch", "2.15.0", ["missing", "missing", "wheel", "missing", "wheel"]),  # no sdist: nothing to build
        pkg(5, "weird", "1.0; rm -rf /", ["source"] * 5),  # not a version: never planned
        pkg(6, "musly", "1.0", ["wheel", "wheel", "wheel", "source", "source"]),  # linux-x86_64 isn't a lab target
    ],
}

GOOD = {
    "schema": 1,
    "name": "tiktoken",
    "version": "0.14.0",
    "target": "3.14 win-arm64",
    "runner": "windows-11-arm",
    "python": "3.14.2",
    "outcome": "built",
    "seconds": 87,
    "import": "ok",
    "gil": None,
    "reason": None,
    "excerpt": "",
    "toolchains": ["cl"],
    "date": "2026-11-15",
}
JOB = {
    "id": "0",
    "name": "tiktoken",
    "version": "0.14.0",
    "target": "3.14 win-arm64",
    "runner": "windows-11-arm",
    "python": "3.14",
    "rank": 2,
}


class Plan(unittest.TestCase):
    def test_most_downloaded_first_taking_turns_between_targets(self):
        jobs = build_lab.plan(COVERAGE, None, 10, "2026-11-15")
        got = [(j["name"], j["target"]) for j in jobs]
        self.assertEqual(
            got,
            [
                ("cryptography", "3.14 win-arm64"),
                ("tiktoken", "3.14t win-arm64"),
                ("orjson", "3.14t linux-x86_64"),
                ("musly", "3.14 musl-aarch64"),
                ("tiktoken", "3.14 win-arm64"),
                ("orjson", "3.14t win-arm64"),
            ],
        )
        self.assertEqual([j["id"] for j in jobs], [str(i) for i in range(6)])
        self.assertEqual(jobs[0]["runner"], "windows-11-arm")
        self.assertEqual(jobs[3]["image"], "python:3.14-alpine")
        self.assertNotIn("image", jobs[0])

    def test_limit_and_recent_results(self):
        self.assertEqual(len(build_lab.plan(COVERAGE, None, 2, "2026-11-15")), 2)
        recent = {"results": [dict(GOOD, name="Cryptography", version="50.0.2", date="2026-10-01")]}
        recent["results"][0]["target"] = "3.14 win-arm64"
        got = [(j["name"], j["target"]) for j in build_lab.plan(COVERAGE, recent, 10, "2026-11-15")]
        self.assertNotIn(("cryptography", "3.14 win-arm64"), got)
        # After REFRESH_DAYS it's built again; a new release is built at once.
        self.assertIn(
            ("cryptography", "3.14 win-arm64"),
            [(j["name"], j["target"]) for j in build_lab.plan(COVERAGE, recent, 10, "2027-03-01")],
        )
        older = {"results": [dict(GOOD, name="cryptography", version="49.0.0", date="2026-11-14")]}
        self.assertIn(
            ("cryptography", "3.14 win-arm64"),
            [(j["name"], j["target"]) for j in build_lab.plan(COVERAGE, older, 10, "2026-11-15")],
        )


class Problems(unittest.TestCase):
    def test_a_good_result_passes(self):
        self.assertEqual(build_lab.problems(GOOD, JOB), [])
        failed = dict(
            GOOD,
            outcome="failed",
            seconds=283,
            **{"import": None},
            reason="needs OpenSSL",
            excerpt="error: OpenSSL not found",
        )
        self.assertEqual(build_lab.problems(failed, JOB), [])
        ft_job = dict(JOB, target="3.14t win-arm64", python="3.14t")
        ft = dict(GOOD, target="3.14t win-arm64", python="3.14.2t", gil="re-enabled")
        self.assertEqual(build_lab.problems(ft, ft_job), [])

    def test_a_result_for_another_pair_is_rejected(self):
        # A build can rewrite its own result file: it must not speak for another package, version or target.
        for key, value in [
            ("name", "cryptography"),
            ("version", "0.15.0"),
            ("target", "3.14t win-arm64"),
            ("runner", "ubuntu-24.04"),
        ]:
            self.assertTrue(build_lab.problems(dict(GOOD, **{key: value}), JOB), key)

    def test_shape_and_vocabulary(self):
        bad = [
            dict(GOOD, extra=1),
            {k: v for k, v in GOOD.items() if k != "date"},
            dict(GOOD, outcome="great"),
            dict(GOOD, seconds=-1),
            dict(GOOD, seconds=True),
            dict(GOOD, seconds=10**9),
            dict(GOOD, **{"import": None}),  # built, but no import result
            dict(GOOD, outcome="failed", reason="needs OpenSSL"),  # failed with an import result
            dict(GOOD, reason="needs OpenSSL"),  # a reason on a build that worked
            dict(GOOD, outcome="failed", **{"import": None}, reason="made up"),
            dict(GOOD, gil="kept-off"),  # not a free-threaded job
            dict(GOOD, excerpt="\x1b[31merror"),
            dict(GOOD, excerpt="é"),
            dict(GOOD, excerpt="x" * 401),
            dict(GOOD, toolchains=["rustc", "rustc"]),
            dict(GOOD, toolchains=["curl"]),
            dict(GOOD, python="3.13.1"),
            dict(GOOD, python="3.14.2t"),
            dict(GOOD, date="yesterday"),
            "not an object",
        ]
        for result in bad:
            self.assertTrue(build_lab.problems(result, JOB), result)
        ft_job = dict(JOB, target="3.14t win-arm64", python="3.14t")
        ft = dict(GOOD, target="3.14t win-arm64", python="3.14.2t")
        self.assertTrue(build_lab.problems(ft, ft_job))  # imported on free-threaded Python, but no GIL answer
        self.assertTrue(build_lab.problems(dict(ft, gil="maybe"), ft_job))
        self.assertTrue(build_lab.problems(dict(ft, **{"import": "failed"}, gil="kept-off"), ft_job))


class Merge(unittest.TestCase):
    def test_merge_keeps_one_entry_per_package_and_target(self):
        old = {
            "results": [
                dict(GOOD, version="0.13.0", date="2026-08-01"),
                dict(GOOD, name="orjson", version="3.13.0", target="3.14 win-arm64"),
            ]
        }
        jobs = [JOB, dict(JOB, id="1", name="cryptography", version="50.0.2")]
        results = {"0": GOOD, "1": dict(GOOD, name="cryptography", version="49.0.0")}  # 1 lies about its version
        merged, notes = build_lab.merge(old, results, jobs, "2026-11-15")
        got = {(r["name"], r["version"]) for r in merged["results"]}
        self.assertEqual(got, {("tiktoken", "0.14.0"), ("orjson", "3.13.0")})
        self.assertEqual(merged["updated"], "2026-11-15")
        self.assertEqual(len(notes), 1)
        self.assertIn("rejected", notes[0])
        missing, notes = build_lab.merge(None, {}, [JOB], "2026-11-15")
        self.assertEqual(missing["results"], [])
        self.assertIn("no result", notes[0])

    def test_merge_command_reads_only_planned_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "results" / "result-0").mkdir(parents=True)
            (tmp / "results" / "result-0" / "result.json").write_text(json.dumps(GOOD), encoding="utf-8")
            (tmp / "results" / "result-9").mkdir()
            (tmp / "results" / "result-9" / "result.json").write_text(
                json.dumps(dict(GOOD, name="x")), encoding="utf-8"
            )
            (tmp / "plan.json").write_text(json.dumps([JOB]), encoding="utf-8")
            builds = tmp / "builds.json"
            self.assertEqual(build_lab.main(["merge", str(builds), str(tmp / "results"), str(tmp / "plan.json")]), 0)
            data = json.loads(builds.read_text(encoding="utf-8"))
            self.assertEqual([r["name"] for r in data["results"]], ["tiktoken"])


class Helpers(unittest.TestCase):
    def test_classify(self):
        cases = {
            "error: failed to run custom build command for `openssl-sys v0.9`": "needs OpenSSL",
            "Cargo, the Rust package manager, is not installed or is not on PATH.": "needs a Rust toolchain",
            "error: Microsoft Visual C++ 14.0 or greater is required.": "needs a C/C++ compiler",
            "src/x.c:1:10: fatal error: libpq-fe.h: No such file or directory": "needs a system library",
            "x.c(12): error C2065: undeclared identifier": "compile error",
            "it just stopped": "other",
        }
        for log, reason in cases.items():
            self.assertEqual(build_lab.classify(log), reason, log)

    def test_excerpt_is_short_printable_ascii(self):
        log = "ok\n\x1b[31merror: café failed\x1b[0m\n" + "error: " + "x" * 1000
        text = build_lab.excerpt(log)
        self.assertLessEqual(len(text), build_lab.MAX_EXCERPT)
        self.assertTrue(all(32 <= ord(c) < 127 for c in text))
        self.assertEqual(
            build_lab.problems(dict(GOOD, outcome="failed", **{"import": None}, reason="other", excerpt=text), JOB), []
        )

    def test_top_level_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "x-1.0-cp314-cp314-win_arm64.whl"
            with zipfile.ZipFile(wheel, "w") as z:
                for name in [
                    "tiktoken/__init__.py",
                    "tiktoken/core.py",
                    "tiktoken_ext/openai_public.py",
                    "_tiktoken.cp314-win_arm64.pyd",
                    "x-1.0.dist-info/RECORD",
                    "helper.py",
                ]:
                    z.writestr(name, "")
            self.assertEqual(build_lab.top_level_modules(wheel), ["tiktoken", "helper"])
            only_private = Path(tmp) / "y-1.0-cp314-cp314-win_arm64.whl"
            with zipfile.ZipFile(only_private, "w") as z:
                z.writestr("_cffi_backend.cpython-314t-x86_64-linux-gnu.so", "")
            self.assertEqual(build_lab.top_level_modules(only_private), ["_cffi_backend"])

    def test_run_refuses_what_the_plan_never_makes(self):
        self.assertEqual(
            build_lab.main(
                [
                    "run",
                    "--name",
                    "x; rm",
                    "--version",
                    "1",
                    "--target",
                    "3.14 win-arm64",
                    "--runner",
                    "r",
                    "--out",
                    "/dev/null",
                ]
            ),
            2,
        )
        self.assertEqual(
            build_lab.main(
                [
                    "run",
                    "--name",
                    "x",
                    "--version",
                    "1",
                    "--target",
                    "3.14 amiga",
                    "--runner",
                    "r",
                    "--out",
                    "/dev/null",
                ]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
