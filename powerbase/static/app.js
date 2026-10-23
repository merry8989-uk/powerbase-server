/* Powerbase dashboard — chart helpers and small UI behaviour. */

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function withAlpha(color, alpha) {
  // Only #rrggbb can be re-alpha'd cheaply; anything else is used as-is.
  if (/^#[0-9a-f]{6}$/i.test(color)) {
    const r = parseInt(color.slice(1, 3), 16);
    const g = parseInt(color.slice(3, 5), 16);
    const b = parseInt(color.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  return color;
}

function baseOptions() {
  const dim = cssVar('--dim', '#667085');
  const border = cssVar('--border', 'rgba(0,0,0,0.1)');
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: cssVar('--surface', '#fff'),
        titleColor: cssVar('--text', '#000'),
        bodyColor: cssVar('--text', '#000'),
        borderColor: border,
        borderWidth: 1,
        padding: 10,
        displayColors: false,
      },
    },
    scales: {
      x: {
        grid: { display: false },
        border: { color: border },
        ticks: { color: dim, font: { size: 11 }, maxRotation: 0, autoSkipPadding: 16 },
      },
      y: {
        beginAtZero: true,
        grid: { color: border, drawTicks: false },
        border: { display: false },
        ticks: { color: dim, font: { size: 11 }, precision: 0 },
      },
    },
  };
}

function lineChart(canvasId, labels, values, label, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === 'undefined') return;
  const stroke = color === 'var(--bad)' ? cssVar('--bad', '#b91c1c') : cssVar('--accent', '#0f766e');
  new Chart(canvas, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: label,
        data: values,
        borderColor: stroke,
        backgroundColor: withAlpha(stroke, 0.14),
        fill: true,
        tension: 0.32,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
      }],
    },
    options: baseOptions(),
  });
}

function barChart(canvasId, labels, values, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === 'undefined') return;
  const stroke = color || cssVar('--accent', '#0f766e');
  new Chart(canvas, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: withAlpha(stroke, 0.55),
        borderColor: stroke,
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: Object.assign(baseOptions(), { indexAxis: 'y' }),
  });
}
