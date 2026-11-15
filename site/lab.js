"use strict";
// The build lab section: reads data/builds.json (scripts/build_lab.py) and shows, for packages with no wheel on a
// platform, whether they build from source there and work. Everything from the file goes in as text, never HTML.
(function () {
  const ORDER = ["gil", "fails", "broken", "partial", "slow", "works"];
  const PILL = { works: "s-wheel", gil: "s-source", partial: "s-source", slow: "s-source", broken: "s-missing", fails: "s-missing" };
  const LABEL = { works: "works", gil: "GIL back on", partial: "no compiled part", broken: "doesn't import", fails: "fails", slow: "too slow" };
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "text") e.textContent = v;
      else if (k === "class") e.className = v;
      else e.setAttribute(k, v);
    }
    for (const kid of kids) if (kid != null) e.append(kid);
    return e;
  };
  const safeName = (n) => /^[A-Za-z0-9._-]{1,100}$/.test(n);

  fetch("data/builds.json")
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      const results = (data && Array.isArray(data.results) ? data.results : []).filter((r) => safeName(r.name));
      if (!results.length) return;
      const rows = results.map((r) => ({ r, v: labVerdict(r) }));
      const targets = [...new Set(results.map((r) => r.target))].sort();
      const count = (t, kind) => rows.filter((x) => x.r.target === t && x.v.kind === kind).length;
      document.getElementById("lab-note").textContent =
        `Each week, the most-downloaded packages that have no wheel for a platform are built from their source on that platform, ` +
        `on GitHub's own runners, the way pip would, then installed and imported. Those runners come with compilers and many system libraries preinstalled, so a build that works there can still need setup on your machine. On free-threaded Python, the lab also checks ` +
        `whether importing the package turned the GIL back on. Nothing built is published. ${results.length} results, ` +
        `updated ${data.updated}.`;
      document.getElementById("lab-summary").replaceChildren(
        ...targets.map((t) =>
          el("li", {},
            el("span", { class: "pname", text: t }),
            el("span", { class: "lab-counts" },
              ...ORDER.filter((k) => count(t, k)).map((k) => el("span", { class: `status ${PILL[k]}`, text: `${count(t, k)} ${LABEL[k]}` })),
            ),
          ),
        ),
      );
      rows.sort((a, b) => a.r.target.localeCompare(b.r.target) || ORDER.indexOf(a.v.kind) - ORDER.indexOf(b.v.kind) || a.r.name.localeCompare(b.r.name));
      const head = el("thead", {}, el("tr", {}, ...["Package", "Target", "From source", "Details"].map((h) => el("th", { scope: "col", text: h }))));
      const body = el("tbody", {}, ...rows.map(({ r, v }) => {
        const detail = r.excerpt ? el("details", {}, el("summary", { text: "log" }), el("code", { text: r.excerpt })) : null;
        const where = `${r.runner}, Python ${r.python}, ${r.date}`;
        return el("tr", {},
          el("td", { class: "pkg" }, el("a", { href: `p/${encodeURIComponent(r.name.toLowerCase().replace(/[-_.]+/g, "-"))}/`, text: r.name }), el("span", { class: "ver", text: r.version })),
          el("td", { text: r.target }),
          el("td", {}, el("span", { class: `status ${PILL[v.kind]}`, text: LABEL[v.kind] }), " ", v.text),
          el("td", { class: "lab-where" }, where, detail),
        );
      }));
      document.getElementById("lab-table").replaceChildren(head, body);
      document.getElementById("lab-section").hidden = false;
    })
    .catch(() => {});
})();
