// The repo checker's worker: loads Pyodide and wheelgap, fetches the repo's files from GitHub and the fixes' release
// lists from PyPI (all from the browser, nothing goes through a server of ours), and runs driver.py on them.
// Messages in: {id, type: "load", repo} | {id, type: "check", lockfile, options}. Out: {id, type, ...}.

import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/pyodide.mjs";
import { rawUrl } from "./check-logic.mjs";

// The wheelgap release the page runs; tests/test_check_driver.py tests driver.py against the same one.
export const WHEELGAP = "wheelgap==0.10.0";
const MAX_FILE = 25 * 1024 * 1024;
const MAX_FIX_LOOKUPS = 80;

let ready = null; // the driver module, once Pyodide and wheelgap are in
let repo = null; // the loaded repo: {owner, name, ref, root}
const releases = new Map(); // PyPI release lists by name, kept across checks

const post = (msg) => self.postMessage(msg);

async function start() {
  post({ type: "progress", stage: "python" });
  const py = await loadPyodide();
  post({ type: "progress", stage: "wheelgap" });
  await py.loadPackage("micropip");
  await py.pyimport("micropip").install(WHEELGAP);
  py.FS.writeFile("/home/pyodide/driver.py", await (await fetch(new URL("./driver.py", import.meta.url))).text());
  const driver = py.pyimport("driver");
  const version = py.runPython("import wheelgap; wheelgap.__version__");
  post({ type: "ready", wheelgap: version });
  return { py, driver };
}

class Failure extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

async function getJson(url) {
  let r;
  try {
    r = await fetch(url, { headers: { Accept: "application/vnd.github+json" } });
  } catch {
    throw new Failure("network", "Couldn't reach GitHub. Check your connection and try again.");
  }
  if (r.status === 404) throw new Failure("not-found", "No such public repository (or branch). Private repos can't be read here: run uvx wheelgap in your checkout instead.");
  if (r.status === 403 || r.status === 429) {
    const reset = Number(r.headers.get("x-ratelimit-reset"));
    const when = reset ? new Date(reset * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "an hour";
    throw new Failure("rate-limit", `GitHub's limit for anonymous requests from your network (60 an hour) is used up. Try again after ${when}, or run uvx wheelgap locally.`);
  }
  if (!r.ok) throw new Failure("github", `GitHub answered ${r.status}. Try again in a minute.`);
  return r.json();
}

async function getText(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Failure("file", `Couldn't fetch ${url.split("/").slice(6).join("/")} (${r.status}).`);
  const len = Number(r.headers.get("content-length"));
  if (len > MAX_FILE) throw new Failure("file", `${url.split("/").pop()} is over 25 MB, too big to check in the browser.`);
  return new Uint8Array(await r.arrayBuffer());
}

// Runs fn over items, at most `limit` at a time.
async function pool(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function lane() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
  return out;
}

function write(py, root, path, bytes) {
  const full = `${root}/${path}`;
  py.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
  py.FS.writeFile(full, bytes);
}

async function load(r) {
  const { py, driver } = await ready;
  const ref = r.ref || "HEAD";
  post({ type: "progress", stage: "tree" });
  const tree = await getJson(
    `https://api.github.com/repos/${r.owner}/${r.name}/git/trees/${encodeURIComponent(ref)}?recursive=1`,
  );
  const paths = (tree.tree || []).filter((e) => e.type === "blob").map((e) => e.path);
  const plan = JSON.parse(driver.plan(JSON.stringify(paths)));
  const root = `/repos/${r.owner}/${r.name}/${Date.now()}`;
  const files = [...plan.workflows, ...plan.dockerfiles, ...(plan.python_version ? [plan.python_version] : [])];
  post({ type: "progress", stage: "files", count: files.length });
  const unread = [];
  await pool(files, 8, async (p) => {
    try {
      write(py, root, p, await getText(rawUrl(r, ref, p)));
    } catch (e) {
      unread.push({ path: p, line: null, reason: e.message });
    }
  });
  py.FS.mkdirTree(root);
  const detected = JSON.parse(driver.detect(root));
  detected.unreadable.push(...unread);
  repo = { ...r, ref, root, fetched: new Set() };
  return { plan, detected, truncated: Boolean(tree.truncated), ref };
}

async function check(lockfile, options) {
  const { py, driver } = await ready;
  if (!repo) throw new Failure("state", "Load a repository first.");
  if (!repo.fetched.has(lockfile)) {
    post({ type: "progress", stage: "lockfile", path: lockfile });
    write(py, repo.root, lockfile, await getText(rawUrl(repo, repo.ref, lockfile)));
    repo.fetched.add(lockfile);
  }
  post({ type: "progress", stage: "checking" });
  const result = JSON.parse(driver.check(repo.root, lockfile, JSON.stringify(options)));
  return result;
}

// Looks up the gap rows' packages on PyPI (most gaps first) and asks the driver for each one's nearest fix.
async function fixes(rows) {
  const { driver } = await ready;
  const names = [...new Set([...rows].sort((a, b) => b.gaps.length - a.gaps.length).map((r) => r.name))];
  const todo = names.filter((n) => !releases.has(n)).slice(0, MAX_FIX_LOOKUPS);
  await pool(todo, 8, async (n) => {
    try {
      const r = await fetch(`https://pypi.org/pypi/${encodeURIComponent(n)}/json`);
      releases.set(n, r.ok ? (await r.json()).releases ?? null : null);
    } catch {
      releases.set(n, null);
    }
  });
  const have = Object.fromEntries(names.filter((n) => releases.get(n)).map((n) => [n, releases.get(n)]));
  return JSON.parse(driver.fixes(JSON.stringify(have), JSON.stringify(rows)));
}

self.onmessage = async ({ data }) => {
  const { id, type } = data;
  try {
    if (type === "load") post({ id, type: "loaded", ...(await load(data.repo)) });
    else if (type === "check") {
      const result = await check(data.lockfile, data.options);
      post({ id, type: "result", result });
      if (!result.error && result.rows.length) post({ id, type: "fixes", fixes: await fixes(result.rows) });
    }
  } catch (e) {
    post({ id, type: "error", kind: e.kind || "internal", message: e.kind ? e.message : `Something went wrong: ${e.message || e}` });
  }
};

ready = start();
ready.catch((e) => post({ type: "error", kind: "startup", message: `Couldn't start Python in your browser: ${e.message || e}` }));
