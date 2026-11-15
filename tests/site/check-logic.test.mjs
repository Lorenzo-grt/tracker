// node --test tests/site/check-logic.test.mjs: the repo checker's pure helpers (site/check/check-logic.mjs).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parseRepo, rawUrl, pickLockfile, toQuery, fromQuery, sortPythons, freeThreaded, fixFor, rowsFor, headline,
  cliCommand, fixPrompt,
} from "../../site/check/check-logic.mjs";

test("parseRepo reads the forms people paste", () => {
  const plain = { owner: "fastapi", name: "fastapi", ref: null, dir: null };
  assert.deepEqual(parseRepo("fastapi/fastapi"), plain);
  assert.deepEqual(parseRepo("https://github.com/fastapi/fastapi"), plain);
  assert.deepEqual(parseRepo("github.com/fastapi/fastapi.git/"), plain);
  assert.deepEqual(parseRepo("git@github.com:fastapi/fastapi.git"), plain);
  assert.deepEqual(parseRepo("https://www.github.com/fastapi/fastapi?tab=readme#top"), plain);
  assert.deepEqual(parseRepo("https://github.com/o/r/tree/dev/backend/api"), { owner: "o", name: "r", ref: "dev", dir: "backend/api" });
  assert.deepEqual(parseRepo("https://github.com/o/r/blob/main/backend/uv.lock"), { owner: "o", name: "r", ref: "main", dir: "backend" });
  assert.deepEqual(parseRepo("https://github.com/o/r/blob/main/uv.lock"), { owner: "o", name: "r", ref: "main", dir: null });
});

test("parseRepo refuses what isn't a repo", () => {
  for (const bad of ["", "fastapi", "https://gitlab.com/a", "a b/c", "../x", "o/..", "o/r/tree/main/../../etc", "javascript:alert(1)", "o/r/tree/ma in/x"]) {
    assert.equal(parseRepo(bad), null, bad);
  }
});

test("rawUrl encodes each path segment", () => {
  assert.equal(rawUrl({ owner: "o", name: "r" }, "HEAD", "a b/uv.lock"), "https://raw.githubusercontent.com/o/r/HEAD/a%20b/uv.lock");
  assert.equal(rawUrl({ owner: "o", name: "r" }, "feat/x", "uv.lock"), "https://raw.githubusercontent.com/o/r/feat/x/uv.lock");
});

test("pickLockfile prefers the URL's folder, then the first readable one", () => {
  const locks = [
    { path: "requirements.txt", offline: false },
    { path: "uv.lock", offline: true },
    { path: "backend/uv.lock", offline: true },
  ];
  assert.equal(pickLockfile(locks, null).path, "uv.lock");
  assert.equal(pickLockfile(locks, "backend").path, "backend/uv.lock");
  assert.equal(pickLockfile([{ path: "requirements.txt", offline: false }], null).path, "requirements.txt");
  assert.equal(pickLockfile([], null), null);
});

test("a result's query round-trips, and unknown values are dropped", () => {
  const state = { repo: { owner: "o", name: "r", ref: "dev" }, lockfile: "backend/uv.lock", pythons: ["3.14", "3.14t"], platforms: ["linux-aarch64", "win-arm64"] };
  const q = toQuery(state);
  assert.equal(q, "repo=o/r&ref=dev&lock=backend/uv.lock&py=3.14,3.14t&t=linux-aarch64,win-arm64");
  const back = fromQuery(`?${q}`);
  assert.deepEqual(back, { repo: { owner: "o", name: "r", ref: "dev", dir: null }, lockfile: "backend/uv.lock", pythons: ["3.14", "3.14t"], platforms: ["linux-aarch64", "win-arm64"] });
  const odd = fromQuery("?repo=o/r&lock=../x&py=3.14,2.7,evil&t=linux-x86_64,amiga");
  assert.equal(odd.lockfile, null);
  assert.deepEqual(odd.pythons, ["3.14"]);
  assert.deepEqual(odd.platforms, ["linux-x86_64"]);
  assert.equal(fromQuery("?repo=nope"), null);
  assert.equal(fromQuery(""), null);
});

test("Pythons sort with each free-threaded build after its own", () => {
  assert.deepEqual(sortPythons(["3.15t", "3.13", "3.15", "3.14t", "3.14", "3.14"]), ["3.13", "3.14", "3.14t", "3.15", "3.15t"]);
  assert.equal(freeThreaded(["3.12", "3.14"]), "3.14t");
  assert.equal(freeThreaded(["3.10", "3.11"]), "3.13t"); // free-threading starts at 3.13
  assert.equal(freeThreaded(["3.14", "3.14t"]), null);
});

const row = (name, version, gaps, statuses = {}) => ({ name, version, gaps, keep: [], statuses });

test("fixFor says one release, some, none, or not looked up", () => {
  const r = row("numpy", "1.26.4", ["3.14 linux-x86_64", "3.14 win-arm64"]);
  assert.deepEqual(fixFor(r, null), { kind: "unknown", text: "" });
  assert.deepEqual(fixFor(r, { "numpy==1.26.4": { found: {}, loses: [] } }), { kind: "none", text: "no newer release has a wheel" });
  const all = fixFor(r, { "numpy==1.26.4": { found: { "3.14 linux-x86_64": "2.3.2", "3.14 win-arm64": "2.3.2" }, loses: [] } });
  assert.deepEqual(all, { kind: "all", version: "2.3.2", text: "2.3.2 (a new major version)" });
  const some = fixFor(r, { "numpy==1.26.4": { found: { "3.14 linux-x86_64": "1.26.5" }, loses: [] } });
  assert.deepEqual(some, { kind: "some", version: "1.26.5", text: "1.26.5 for 1 of 2 gaps" });
});

const result = {
  lockfile: "uv.lock",
  packages: 10,
  outside_lock: ["3.15"],
  targets: [
    { label: "3.14 linux-x86_64", python: "3.14", platform: "linux-x86_64" },
    { label: "3.14 win-arm64", python: "3.14", platform: "win-arm64" },
    { label: "3.15 linux-x86_64", python: "3.15", platform: "linux-x86_64" },
  ],
  rows: [
    row("a", "1.0", ["3.14 win-arm64"], { "3.14 linux-x86_64": "wheel", "3.14 win-arm64": "source", "3.15 linux-x86_64": "missing" }),
    row("b", "2.0", ["3.14 linux-x86_64", "3.14 win-arm64"], { "3.14 linux-x86_64": "missing", "3.14 win-arm64": "missing", "3.15 linux-x86_64": "wheel" }),
  ],
};

test("rowsFor lists one Python's gaps, most first; headline counts distinct packages on counted targets", () => {
  assert.deepEqual(rowsFor(result, "3.14").map((r) => [r.name, r.here.length]), [["b", 2], ["a", 1]]);
  assert.deepEqual(rowsFor(result, "3.15"), []); // outside the lock: check() reports no gaps there
  assert.deepEqual(headline(result), { gaps: 2, packages: 10, targets: 2 });
});

test("the CLI command and the fix prompt reproduce the check", () => {
  assert.equal(cliCommand(result, ["3.14t", "3.14"], ["linux-x86_64", "win-arm64"]), "uvx wheelgap uv.lock -p 3.14,3.14t -t linux-x86_64,win-arm64");
  assert.equal(cliCommand({ lockfile: "my app/uv.lock" }, ["3.14"], ["linux-x86_64"]), "uvx wheelgap 'my app/uv.lock' -p 3.14 -t linux-x86_64");
  const fixes = { "a==1.0": { found: { "3.14 win-arm64": "1.1" }, loses: [] }, "b==2.0": { found: {}, loses: [] } };
  const prompt = fixPrompt("o/r", result, fixes, ["3.14"], ["linux-x86_64", "win-arm64"]);
  assert.match(prompt, /- a 1\.0 -> 1\.1\n/);
  assert.match(prompt, /No newer release has a wheel for these[^\n]*\n- b 2\.0: 3\.14 linux-x86_64, 3\.14 win-arm64/);
  assert.match(prompt, /uvx wheelgap uv\.lock -p 3\.14 -t linux-x86_64,win-arm64/);
  assert.doesNotMatch(prompt, /weren't looked up/);
  assert.match(fixPrompt("o/r", result, null, ["3.14"], ["linux-x86_64"]), /weren't looked up on PyPI/);
});
