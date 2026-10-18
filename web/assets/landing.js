// FloodAlpha landing page behaviour: hero bars, storm metric toggle, scroll reveal (mobile nav: nav.js).
// Storm numbers are copied from README.md "Results" (generated 2026-10-17 21:55 UTC from data/processed/).
(() => {
  "use strict";
  document.documentElement.classList.add("js");

  const STORMS = Object.freeze([
    { name: "Harvey 2017", km2: 1368, hours: 85, cov: 100 },
    { name: "Irma 2017", km2: 202, hours: 24, cov: 100 },
    { name: "Florence 2018", km2: 69, hours: 3, cov: 93 },
    { name: "Michael 2018", km2: 32, hours: 10, cov: 100 },
    { name: "Laura 2020", km2: 221, hours: 119, cov: 100 },
    { name: "Sally 2020", km2: 7, hours: 6, cov: 100 },
    { name: "Ida 2021", km2: 8, hours: 113, cov: 41 },
    { name: "Ian 2022", km2: 242, hours: 106, cov: 99 },
    { name: "Idalia 2023", km2: 27, hours: 65, cov: 100 },
    { name: "Helene 2024", km2: 18, hours: 49, cov: 65 },
    { name: "Milton 2024", km2: 101, hours: 30, cov: 76 },
    { name: "Isaias 2026", km2: 3, hours: 28, cov: 52, flagship: true },
  ]);

  const METRICS = Object.freeze({
    km2: { label: "Flooded area from the first post-storm passes", fmt: (v) => `${v.toLocaleString("en-US")} km²` },
    hours: { label: "Hours from landfall to the first published radar pass", fmt: (v) => `${v} h` },
    cov: { label: "Share of the storm AOI covered by post-storm passes", fmt: (v) => `${v} %` },
  });

  // Decorative hero columns: heights and colours only, no data meaning.
  const HERO_BARS = Object.freeze([
    [38, "#3fd6c6"], [52, "#3fd6c6"], [44, "#5fe0d2"], [70, "#3fd6c6"], [58, "#7aa2ff"],
    [82, "#9fb6ff"], [64, "#c7cfe0"], [48, "#ff8a4c"], [76, "#ff6f50"], [90, "#ff8a4c"],
    [60, "#ff6f50"], [72, "#ff8a4c"], [46, "#ff6f8e"], [56, "#ff8a4c"],
  ]);

  function renderHeroBars() {
    const host = document.getElementById("bars");
    if (!host) return;
    const frag = document.createDocumentFragment();
    HERO_BARS.forEach(([h, c], i) => {
      const bar = document.createElement("span");
      bar.className = "bar";
      bar.style.setProperty("--h", `${h}%`);
      bar.style.setProperty("--c", c);
      bar.style.setProperty("--d", `${i * 60}ms`);
      frag.appendChild(bar);
    });
    host.replaceChildren(frag);
  }

  function renderStormChart(metricKey) {
    const host = document.getElementById("storm-chart");
    const label = document.getElementById("metric-label");
    const metric = METRICS[metricKey];
    if (!host || !metric) return;
    const max = Math.max(...STORMS.map((s) => s[metricKey]));
    const rows = STORMS.map((s) => {
      const li = document.createElement("li");
      if (s.flagship) li.className = "flag";
      const name = document.createElement("span");
      name.textContent = s.name;
      const track = document.createElement("div");
      track.className = "track";
      const fill = document.createElement("i");
      fill.style.setProperty("--w", `${Math.max((s[metricKey] / max) * 100, 0.8)}%`);
      track.appendChild(fill);
      const value = document.createElement("b");
      value.textContent = metric.fmt(s[metricKey]);
      li.append(name, track, value);
      return li;
    });
    host.replaceChildren(...rows);
    if (label) label.textContent = metric.label;
  }

  function wireToggle() {
    const buttons = Array.from(document.querySelectorAll(".toggle [data-metric]"));
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        buttons.forEach((b) => b.setAttribute("aria-selected", String(b === btn)));
        renderStormChart(btn.dataset.metric);
      });
    });
  }

  function wireReveal() {
    const items = document.querySelectorAll(".reveal");
    if (!("IntersectionObserver" in window)) {
      items.forEach((el) => el.classList.add("in"));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach((el) => io.observe(el));
  }

  renderHeroBars();
  renderStormChart("km2");
  wireToggle();
  wireReveal();
})();
