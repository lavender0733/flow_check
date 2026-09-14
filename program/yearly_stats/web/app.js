const byId = (id) => document.getElementById(id);
const SYSTEM_COLORS = { "620": "#5b9bd5", "630": "#9e80db", "680": "#e6a817" };

// 「更新至」的月份一律由後端從來源檔內容判定（updated_month，格式 YYYYMM）：
// 各系統的表格長相不同，靠檔名猜會在改檔名後失準（680 從「1月~7月」改成
// 「202601-202608」就是這樣卡在 7 月）。
function updateMonth(system) {
  const stamp = Number(system.updated_month) || 0;
  if (stamp < 202601 || stamp > 209912) return "—";
  const text = String(stamp);
  return `${text.slice(0, 4)}/${Number(text.slice(4, 6))}`;
}

function renderPie(systems, target) {
  let degrees = 0;
  const stops = [];
  const legend = byId("pie-legend");
  legend.innerHTML = "";

  systems.forEach((system) => {
    const count = Math.max(0, Number(system.count) || 0);
    const shareOfTarget = target ? Math.min(100, count / target * 100) : 0;
    const color = SYSTEM_COLORS[system.name] || "#8b82a6";
    const next = degrees + shareOfTarget * 3.6;
    stops.push(`${color} ${degrees.toFixed(2)}deg ${next.toFixed(2)}deg`);
    degrees = next;

    const item = document.createElement("div");
    item.innerHTML = `<i style="background:${color}"></i><span>${system.name}</span><b>${shareOfTarget.toFixed(1)}%</b>`;
    legend.appendChild(item);
  });
  if (degrees < 360) stops.push(`#ece7f7 ${degrees.toFixed(2)}deg 360deg`);
  byId("system-pie").style.background = stops.length
    ? `conic-gradient(${stops.join(",")})`
    : "#ece7f7";
}

function render(data) {
  const systems = Array.isArray(data.systems) ? data.systems : [];
  const target = Math.max(0, Number(data.target) || 0);
  const total = Math.max(0, Number(data.total) || 0);
  const percent = target ? total / target * 100 : 0;

  byId("year").textContent = new Date().getFullYear();
  byId("total").textContent = total.toLocaleString();
  byId("target").textContent = target.toLocaleString();
  byId("percent").textContent = percent.toFixed(1) + "%";
  byId("remaining").textContent = Math.max(0, target - total).toLocaleString();
  renderPie(systems, target);

  const box = byId("systems");
  box.innerHTML = "";
  systems.forEach((system) => {
    const count = Math.max(0, Number(system.count) || 0);
    const color = SYSTEM_COLORS[system.name] || "#8b82a6";
    const card = document.createElement("article");
    card.className = "system";
    card.style.setProperty("--system-color", color);
    card.innerHTML = `
      <span class="system-name">系統 ${system.name}</span>
      <strong>${count.toLocaleString()} <small>次</small></strong>
      <span class="system-month">更新至 ${updateMonth(system)}</span>`;
    box.appendChild(card);
  });
}

async function refreshStats() {
  try {
    const response = await fetch("/api/stats");
    const data = await response.json();
    if (!data.ok) throw new Error(data.message || "讀取失敗");
    render(data);
  } catch (error) {
    console.error("讀取統計失敗：", error);
  }
}

refreshStats();
