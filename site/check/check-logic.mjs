// Pure helpers for the repo checker (check.mjs and worker.mjs), kept apart so tests/site/check-logic.test.mjs can run
// them under node with no DOM. Nothing here touches the network.

export const PLATFORMS = [
  ["linux-x86_64", "Linux x86_64"],
  ["linux-aarch64", "Linux ARM64"],
  ["musl-x86_64", "Alpine x86_64"],
  ["musl-aarch64", "Alpine ARM64"],
  ["macos-arm64", "macOS Apple silicon"],
  ["macos-x86_64", "macOS Intel"],
  ["win-amd64", "Windows x64"],
  ["win-arm64", "Windows on ARM"],
];
export const PYTHONS = ["3.13", "3.14", "3.15", "3.14t", "3.15t"];
export const GAP = new Set(["source", "missing", "unknown"]);

const NAME = /^[A-Za-z0-9_.-]{1,100}$/;

// "owner/name", a github.com URL (with /tree/REF/DIR or /blob/REF/FILE), or an SSH remote, to {owner, name, ref, dir}.
// ref and dir are null when the input doesn't name them. Returns null for anything else.
export function parseRepo(input) {
  let s = String(input ?? "").trim();
  if (!s || s.length > 500) return null;
  s = s.replace(/^git@github\.com:/i, "github.com/");
  s = s.replace(/^(https?:\/\/)?(www\.)?github\.com\//i, "");
  s = s.replace(/[?#].*$/, "").replace(/\/+$/, "");
  const parts = s.split("/");
  if (parts.length < 2) return null;
  const owner = parts[0];
  const name = parts[1].replace(/\.git$/i, "");
  if (!NAME.test(owner) || !NAME.test(name) || owner.startsWith(".") || name === "." || name === "..") return null;
  let ref = null;
  let dir = null;
  if ((parts[2] === "tree" || parts[2] === "blob") && parts[3]) {
    ref = decodeURIComponent(parts[3]);
    const rest = parts.slice(4).map(decodeURIComponent);
    if (parts[2] === "blob") rest.pop(); // a file: its folder
    dir = rest.length ? rest.join("/") : null;
  }
  if (ref !== null && !/^[A-Za-z0-9._\/-]{1,200}$/.test(ref)) return null;
  if (dir !== null && (dir.split("/").some((p) => p === ".." || p === "") || dir.length > 300)) return null;
  return { owner, name, ref, dir };
}

// The raw.githubusercontent.com URL of a file in the repo, each path segment encoded.
export function rawUrl(repo, ref, path) {
  const enc = (p) => p.split("/").map(encodeURIComponent).join("/");
  return `https://raw.githubusercontent.com/${repo.owner}/${repo.name}/${enc(ref)}/${enc(path)}`;
}

// The lockfile to start from: the first checkable one in `dir` (the folder the URL named), else plan()'s first
// checkable one (shallowest), else the first of any kind (so the page can say why it can't check it).
export function pickLockfile(lockfiles, dir) {
  const inDir = (l) => dir !== null && l.path === `${dir}/${l.path.split("/").pop()}`;
  return (
    lockfiles.find((l) => l.offline && inDir(l)) ??
    lockfiles.find((l) => l.offline) ??
    lockfiles.find(inDir) ??
    lockfiles[0] ??
    null
  );
}

// Page state <-> URL query, so a result can be shared. Unknown platforms and Pythons are dropped.
export function toQuery(state) {
  const q = new URLSearchParams();
  q.set("repo", `${state.repo.owner}/${state.repo.name}`);
  if (state.repo.ref) q.set("ref", state.repo.ref);
  if (state.lockfile) q.set("lock", state.lockfile);
  if (state.pythons?.length) q.set("py", state.pythons.join(","));
  if (state.platforms?.length) q.set("t", state.platforms.join(","));
  return q.toString().replace(/%2F/g, "/").replace(/%2C/g, ",");
}

export function fromQuery(search) {
  const q = new URLSearchParams(search);
  const repoText = q.get("repo");
  if (!repoText) return null;
  const repo = parseRepo(repoText);
  if (!repo) return null;
  const ref = q.get("ref");
  if (ref && /^[A-Za-z0-9._\/-]{1,200}$/.test(ref)) repo.ref = ref;
  const known = new Set(PLATFORMS.map(([p]) => p));
  const list = (k, ok) => (q.get(k) || "").split(",").filter((v) => ok(v));
  const lock = q.get("lock");
  return {
    repo,
    lockfile: lock && !lock.split("/").includes("..") && lock.length < 300 ? lock : null,
    pythons: list("py", (v) => PYTHONS.includes(v) || /^3\.(1[0-9])t?$/.test(v)),
    platforms: list("t", (v) => known.has(v)),
  };
}

// The Python versions in display order: 3.13, 3.14, 3.14t, 3.15, 3.15t.
export function sortPythons(pythons) {
  const key = (v) => {
    const [maj, min] = v.replace(/t$/, "").split(".").map(Number);
    return maj * 1000 + min * 2 + (v.endsWith("t") ? 1 : 0);
  };
  return [...new Set(pythons)].sort((a, b) => key(a) - key(b));
}

// The free-threaded build of the newest Python picked, for the "+ free-threaded" shortcut (null if it's already in).
export function freeThreaded(pythons) {
  const plain = sortPythons(pythons).filter((v) => !v.endsWith("t"));
  const newest = plain.length ? plain[plain.length - 1] : "3.15";
  const [, min] = newest.split(".").map(Number);
  const ft = `3.${Math.max(min, 13)}t`;
  return pythons.includes(ft) ? null : ft;
}

// One row's fix, for the table and the prompt: {kind, version?, text}.
// kind: "all" (one release fixes every gap), "some" (some gaps have a release), "none", or "unknown" (not looked up).
export function fixFor(row, fixes) {
  const fix = fixes?.[`${row.name}==${row.version}`];
  if (fix === undefined) return { kind: "unknown", text: "" };
  const found = Object.entries(fix.found);
  if (!found.length) return { kind: "none", text: "no newer release has a wheel" };
  const versions = [...new Set(found.map(([, v]) => v))];
  const major = (v) => String(v).split(".")[0];
  const caution = versions.some((v) => major(v) !== major(row.version)) ? " (a new major version)" : "";
  if (found.length === row.gaps.length && versions.length === 1) {
    return { kind: "all", version: versions[0], text: `${versions[0]}${caution}` };
  }
  return {
    kind: "some",
    version: versions[versions.length - 1],
    text: `${versions.join(" or ")}${caution} for ${found.length} of ${row.gaps.length} gaps`,
  };
}

// The gap rows for one Python, with how many of its platforms each fails on.
export function rowsFor(result, python) {
  const labels = result.targets.filter((t) => t.python === python).map((t) => t.label);
  return result.rows
    .map((r) => ({ ...r, here: labels.filter((l) => GAP.has(r.statuses[l]) && r.gaps.includes(l)) }))
    .filter((r) => r.here.length)
    .sort((a, b) => b.here.length - a.here.length || a.name.localeCompare(b.name));
}

// The headline for a result: how many distinct packages have a gap somewhere, out of how many.
export function headline(result) {
  const names = new Set(result.rows.map((r) => r.name));
  const counted = result.targets.filter((t) => !result.outside_lock.includes(t.python));
  return { gaps: names.size, packages: result.packages, targets: counted.length };
}

// The `uvx wheelgap` command that reproduces the result locally.
export function cliCommand(result, pythons, platforms) {
  const lock = /^[A-Za-z0-9._\/-]+$/.test(result.lockfile) ? result.lockfile : `'${result.lockfile.replace(/'/g, "")}'`;
  return `uvx wheelgap ${lock} -p ${sortPythons(pythons).join(",")} -t ${platforms.join(",")}`;
}

// A prompt to paste into Claude Code (or any coding agent): the gaps, the bumps that fix them, and how to confirm.
export function fixPrompt(repoName, result, fixes, pythons, platforms) {
  const bumps = [];
  const partial = [];
  const stuck = [];
  const unchecked = [];
  for (const r of result.rows) {
    const f = fixFor(r, fixes);
    if (f.kind === "all") bumps.push(`- ${r.name} ${r.version} -> ${f.text}`);
    else if (f.kind === "some") partial.push(`- ${r.name} ${r.version} -> ${f.text} (gaps on ${r.gaps.join(", ")})`);
    else if (f.kind === "none") stuck.push(`- ${r.name} ${r.version}: ${r.gaps.join(", ")}`);
    else unchecked.push(`- ${r.name} ${r.version}: ${r.gaps.join(", ")}`);
  }
  const lines = [
    `In ${repoName}, wheelgap found dependencies in ${result.lockfile} with no prebuilt wheel for some of the platforms we target (Python ${sortPythons(pythons).join(", ")} on ${platforms.join(", ")}), so they'd be compiled from source or fail to install.`,
    "",
  ];
  if (bumps.length) lines.push("Bump these to the release named, which has wheels for every target where the pinned one is missing one:", ...bumps, "");
  if (partial.length) lines.push("These have a newer release with wheels for some of the gaps:", ...partial, "");
  if (stuck.length) lines.push("No newer release has a wheel for these (consider dropping the target, another package, or building wheels):", ...stuck, "");
  if (unchecked.length) lines.push("These weren't looked up on PyPI, so check them with `uvx wheelgap --explain --suggest`:", ...unchecked, "");
  lines.push(
    "Check each bump against our version constraints (a new major version may need code changes), update the lockfile with the project's own tool, and confirm with:",
    `  ${cliCommand(result, pythons, platforms)}`,
    "",
    "To let the agent check packages itself next time: claude mcp add wheelgap -- uvx --from wheelgap wheelgap-mcp",
  );
  return lines.join("\n");
}

// The verdict's headline sentence.
export function verdictText(h) {
  const where = h.targets === 1 ? "this target" : `all ${h.targets} targets`;
  if (h.gaps === 0) return `✓ All ${h.packages} packages have a wheel for ${where}`;
  const some = h.targets === 1 ? "this target" : `at least one of the ${h.targets} targets`;
  return `${h.gaps} of ${h.packages} packages ${h.gaps === 1 ? "has" : "have"} no wheel for ${some}`;
}

// "Ready for what's next?" shortcuts: the platforms and Pythons people most often move to, that aren't picked yet.
// Each is {label, pythons, platforms}: the whole selection to switch to.
export function nextSteps(pythons, platforms) {
  const out = [];
  const add = (label, py, plats) => out.push({ label, pythons: sortPythons(py), platforms: PLATFORMS.map(([p]) => p).filter((p) => plats.includes(p)) });
  const ft = freeThreaded(pythons);
  if (ft) add(`Free-threaded ${ft}`, [...pythons, ft], platforms);
  if (!platforms.includes("win-arm64")) add("Windows on ARM", pythons, [...platforms, "win-arm64"]);
  if (!platforms.includes("linux-aarch64")) add("Linux ARM64", pythons, [...platforms, "linux-aarch64"]);
  if (!platforms.includes("musl-x86_64") && !platforms.includes("musl-aarch64")) add("Alpine Linux", pythons, [...platforms, "musl-x86_64", "musl-aarch64"]);
  const newest = PYTHONS.filter((v) => !v.endsWith("t")).at(-1);
  if (!pythons.includes(newest)) add(`Python ${newest}`, [...pythons, newest], platforms);
  const all = PLATFORMS.map(([p]) => p);
  const everyFt = ft ? [...pythons, ft] : pythons;
  if (out.length > 1) add("Everything", everyFt, all);
  return out;
}
