// Dashboard shell: tab bar + hash routing (#tab?key=value) so every view has a shareable URL.
import { h, mount } from "./core.js";
import * as caseStudy from "./case.js";
import * as live from "./live.js";
import * as method from "./method.js";
import { renderBanks, renderMarket, renderValidation } from "./research.js";

const TABS = Object.freeze([
  { id: "case", label: "Storm case study", render: caseStudy.render },
  { id: "live", label: "Live", render: live.render },
  { id: "banks", label: "Bank exposure", render: renderBanks },
  { id: "market", label: "Market test", render: renderMarket },
  { id: "validation", label: "Validation", render: renderValidation },
  { id: "method", label: "Methodology", render: method.render },
]);

function parseHash() {
  const [id, query = ""] = location.hash.replace(/^#/, "").split("?");
  const tab = TABS.find((t) => t.id === id) || TABS[0];
  return { tab, params: Object.fromEntries(new URLSearchParams(query)) };
}

function hashFor(id, params) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  return `#${id}${q.toString() ? `?${q}` : ""}`;
}

const tabBar = document.getElementById("tabs");
const view = document.getElementById("view");

tabBar.replaceChildren(...TABS.map((t) => h("a", { href: `#${t.id}`, role: "tab", "data-tab": t.id }, t.label)));

let renderId = 0;
let currentTab = null;
function route() {
  const { tab, params } = parseHash();
  if (currentTab !== null && currentTab !== tab.id) window.scrollTo({ top: 0, behavior: "instant" });
  currentTab = tab.id;
  for (const a of tabBar.children) {
    const on = a.dataset.tab === tab.id;
    a.setAttribute("aria-selected", String(on));
    if (on) a.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  document.title = `${tab.label} · FloodAlpha`;
  const id = ++renderId;
  const state = {
    ...params,
    go: (next) => { location.hash = hashFor(tab.id, { ...params, ...next }); },
  };
  mount(view, async () => {
    const nodes = await tab.render(state);
    return id === renderId ? nodes : null; // a newer navigation won; drop stale output
  });
}

window.addEventListener("hashchange", route);
route();
