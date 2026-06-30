// Jarvis site — live stats polling + docs scrollspy.

const REFRESH_MS = 15000;

function fmtNumber(n) {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US");
}

async function refreshStats() {
  try {
    const res = await fetch("/api/stats", { cache: "no-store" });
    if (!res.ok) throw new Error("bad response");
    const data = await res.json();

    document.getElementById("core-guilds")?.replaceChildren(fmtNumber(data.guilds));
    document.getElementById("core-users")?.replaceChildren(fmtNumber(data.users));
    const coreLabel = document.getElementById("core-online-label");
    if (coreLabel) coreLabel.textContent = data.online ? "Online" : "Reconnecting";

    document.getElementById("t-status").textContent = data.online ? "Online" : "Reconnecting";
    document.getElementById("t-guilds").textContent = fmtNumber(data.guilds);
    document.getElementById("t-users").textContent = fmtNumber(data.users);
    document.getElementById("t-uptime").textContent = data.uptime_human || "—";
    document.getElementById("t-latency").textContent =
      data.latency_ms !== null && data.latency_ms !== undefined ? `${data.latency_ms} ms` : "—";
  } catch (err) {
    document.getElementById("t-status").textContent = "Unreachable";
    const coreLabel = document.getElementById("core-online-label");
    if (coreLabel) coreLabel.textContent = "Unreachable";
  }
}

refreshStats();
setInterval(refreshStats, REFRESH_MS);

// ── Docs scrollspy: highlight the active category in the sidebar ──────────
const navItems = document.querySelectorAll(".docs-nav-item");
const docSections = document.querySelectorAll(".docs-cat");

if ("IntersectionObserver" in window && navItems.length && docSections.length) {
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const id = entry.target.id;
          navItems.forEach((item) => {
            item.classList.toggle("active", item.getAttribute("href") === `#${id}`);
          });
        }
      });
    },
    { rootMargin: "-15% 0px -70% 0px" }
  );
  docSections.forEach((section) => observer.observe(section));
}