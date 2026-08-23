/* Gemeinsame Diagramm-Helfer fuers Admin-Tool.
 *
 * Bisher steckte derselbe Chart.js-Baukasten dreimal kopiert in
 * overview.html, metrics.html und tenant_detail.html. Neue Seiten
 * benutzen diese Datei; die bestehenden bleiben vorerst, wie sie sind —
 * sie funktionieren, und ein Umbau ohne Testnetz waere Risiko ohne
 * Gegenwert.
 */
(function (global) {
  "use strict";

  var PALETTE = [
    "#3b82f6", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444", "#06b6d4",
    "#ec4899", "#6366f1", "#14b8a6", "#84cc16", "#f97316", "#a855f7"
  ];

  function element(id) {
    var el = document.getElementById(id);
    return el && global.Chart ? el : null;
  }

  var basis = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } }
  };

  /** Zeitreihe mit einer oder mehreren Linien. */
  function linie(id, labels, reihen) {
    var el = element(id);
    if (!el) return;
    new global.Chart(el, {
      type: "line",
      data: {
        labels: labels,
        datasets: reihen.map(function (r, i) {
          return {
            label: r.name,
            data: r.werte,
            borderColor: PALETTE[i % PALETTE.length],
            backgroundColor: "transparent",
            borderWidth: 2,
            tension: 0.3,
            pointRadius: labels.length > 45 ? 0 : 2
          };
        })
      },
      options: Object.assign({}, basis, {
        plugins: { legend: { display: reihen.length > 1, labels: { boxWidth: 12 } } },
        scales: {
          x: { grid: { display: false }, ticks: { font: { size: 10 }, maxTicksLimit: 10 } },
          y: { beginAtZero: true, ticks: { font: { size: 10 }, precision: 0 } }
        }
      })
    });
  }

  /** Liegende Balken — gut fuer Ranglisten mit langen Beschriftungen. */
  function balkenQuer(id, labels, werte) {
    var el = element(id);
    if (!el) return;
    new global.Chart(el, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [{ data: werte, backgroundColor: "#3b82f6", borderRadius: 6 }]
      },
      options: Object.assign({}, basis, {
        indexAxis: "y",
        scales: {
          x: { beginAtZero: true, ticks: { font: { size: 10 }, precision: 0 } },
          y: { ticks: { font: { size: 10 } } }
        }
      })
    });
  }

  /** Anteile — bewusst Doughnut, das liest sich besser als ein Vollkreis. */
  function anteile(id, labels, werte) {
    var el = element(id);
    if (!el) return;
    new global.Chart(el, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [{
          data: werte,
          backgroundColor: labels.map(function (_, i) {
            return PALETTE[i % PALETTE.length];
          })
        }]
      },
      options: Object.assign({}, basis, {
        cutout: "58%",
        plugins: { legend: { position: "right", labels: { boxWidth: 12, font: { size: 11 } } } }
      })
    });
  }

  global.AdminCharts = {
    palette: PALETTE,
    linie: linie,
    balkenQuer: balkenQuer,
    anteile: anteile
  };
})(window);
