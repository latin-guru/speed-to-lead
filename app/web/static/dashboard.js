// speed-to-lead dashboard: draws the charts and refreshes everything every 5 seconds from
// /api/metrics/summary. No framework, no inline script (the page has a strict CSP), and all
// text is set with textContent, so data can never become markup.
(function () {
  "use strict";

  var root = document.getElementById("kpis");
  if (!root || typeof window.Chart === "undefined") return;

  var URL_SUMMARY = root.getAttribute("data-summary-url");
  var TZ = root.getAttribute("data-tz") || undefined;
  var REFRESH_MS = 5000;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var charts = {};
  var timer = null;

  var timeFmt = new Intl.DateTimeFormat(undefined, {
    timeZone: TZ, month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  var dayFmt = new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "short", day: "numeric" });

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function duration(s) {
    if (s === null || s === undefined) return "-";
    if (s < 60) return Math.round(s) + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    if (s < 86400) return (s / 3600).toFixed(1) + "h";
    return (s / 86400).toFixed(1) + "d";
  }

  function pct(x) { return x === null || x === undefined ? "-" : Math.round(x * 100) + "%"; }

  function setText(sel, text) {
    var el = root.querySelector('[data-kpi="' + sel + '"]');
    if (el) el.textContent = text;
  }

  function theme() {
    return {
      series: css("--series-1"), text: css("--text-2"), grid: css("--grid"),
      surface: css("--surface"),
    };
  }

  function baseOptions(t, extra) {
    var o = {
      responsive: true,
      maintainAspectRatio: false,
      animation: reduceMotion ? false : { duration: 300 },
      plugins: { legend: { display: false }, tooltip: { intersect: false, mode: "index" } },
      scales: {
        x: { grid: { display: false }, ticks: { color: t.text }, border: { color: t.grid } },
        y: { beginAtZero: true, grid: { color: t.grid }, ticks: { color: t.text, precision: 0 }, border: { display: false } },
      },
    };
    return Object.assign(o, extra || {});
  }

  function barDataset(t, values, label) {
    return {
      label: label, data: values, backgroundColor: t.series, borderRadius: 4,
      borderSkipped: "start", borderColor: t.surface, borderWidth: 0,
      barPercentage: 0.92, categoryPercentage: 0.92, maxBarThickness: 48,
    };
  }

  function makeCharts() {
    var t = theme();
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = t.text;

    charts.funnel = new Chart(document.getElementById("chart-funnel"), {
      type: "bar",
      data: { labels: [], datasets: [barDataset(t, [], "Leads")] },
      options: baseOptions(t, {
        indexAxis: "y",
        scales: {
          x: { beginAtZero: true, grid: { color: t.grid }, ticks: { color: t.text, precision: 0 }, border: { display: false } },
          y: { grid: { display: false }, ticks: { color: t.text }, border: { color: t.grid } },
        },
      }),
    });
    charts.sources = new Chart(document.getElementById("chart-sources"), {
      type: "bar", data: { labels: [], datasets: [barDataset(t, [], "Leads")] }, options: baseOptions(t),
    });
    var hist = barDataset(t, [], "Leads");
    hist.barPercentage = 1; hist.categoryPercentage = 0.96; // a 2px-ish gap between buckets
    charts.scores = new Chart(document.getElementById("chart-scores"), {
      type: "bar", data: { labels: [], datasets: [hist] }, options: baseOptions(t),
    });
    var trendOpts = baseOptions(t);
    trendOpts.plugins.tooltip.callbacks = {
      label: function (ctx) { return "Median " + duration(ctx.raw === null ? null : ctx.raw * 60); },
    };
    trendOpts.scales.y.ticks.precision = undefined;
    charts.trend = new Chart(document.getElementById("chart-trend"), {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          label: "Median minutes", data: [], borderColor: t.series, backgroundColor: t.series,
          borderWidth: 2, pointRadius: 4, pointHoverRadius: 6, pointBorderColor: t.surface,
          pointBorderWidth: 2, spanGaps: false, tension: 0,
        }],
      },
      options: trendOpts,
    });
  }

  function recolor() {
    var t = theme();
    Chart.defaults.color = t.text;
    Object.keys(charts).forEach(function (k) {
      var c = charts[k];
      c.data.datasets.forEach(function (d) {
        d.backgroundColor = t.series; d.borderColor = k === "trend" ? t.series : t.surface;
        if (k === "trend") d.pointBorderColor = t.surface;
      });
      ["x", "y"].forEach(function (axis) {
        var s = c.options.scales[axis];
        s.ticks.color = t.text;
        if (s.grid && s.grid.display !== false) s.grid.color = t.grid;
        if (s.border && s.border.color) s.border.color = t.grid;
      });
      c.update("none");
    });
  }

  function fillTable(id, headers, rows) {
    var table = document.getElementById(id);
    if (!table) return;
    table.textContent = "";
    var head = table.createTHead().insertRow();
    headers.forEach(function (h) {
      var th = document.createElement("th"); th.scope = "col"; th.textContent = h; head.appendChild(th);
    });
    var body = table.createTBody();
    rows.forEach(function (r) {
      var tr = body.insertRow();
      r.forEach(function (v) { tr.insertCell().textContent = v; });
    });
  }

  function setChart(chart, labels, values) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.update(reduceMotion ? "none" : undefined);
  }

  function render(data) {
    var k = data.kpis;
    setText("leads", String(k.leads));
    setText("leads_delta", "vs " + k.leads_previous + " the week before");
    setText("median_first_response_seconds", duration(k.median_first_response_seconds));
    setText("win_rate", pct(k.win_rate));
    setText("average_score", k.average_score === null ? "-" : String(k.average_score));
    setText("open_tasks", String(k.open_tasks));
    setText("dead_jobs", String(k.dead_jobs));
    setText("dead_status", k.dead_jobs ? "needs attention" : "all clear");
    var tile = root.querySelector('[data-kpi-tile="dead_jobs"]');
    if (tile) tile.classList.toggle("is-alert", k.dead_jobs > 0);

    var steps = data.funnel.steps;
    setChart(charts.funnel, steps.map(function (s) { return s.stage; }), steps.map(function (s) { return s.reached; }));
    var conv = document.getElementById("funnel-conv");
    conv.textContent = "";
    steps.slice(1).forEach(function (s, i) {
      var li = document.createElement("li");
      li.appendChild(document.createTextNode(steps[i].stage + " → " + s.stage + ": "));
      var b = document.createElement("strong"); b.textContent = pct(s.conversion); li.appendChild(b);
      conv.appendChild(li);
    });
    var lostLi = document.createElement("li");
    lostLi.textContent = "lost: " + data.funnel.lost;
    conv.appendChild(lostLi);
    fillTable("table-funnel", ["Stage", "Reached", "From previous"],
      steps.map(function (s) { return [s.stage, String(s.reached), pct(s.conversion)]; }));

    setChart(charts.sources, data.sources.map(function (s) { return s.label; }), data.sources.map(function (s) { return s.count; }));
    fillTable("table-sources", ["Source", "Leads"], data.sources.map(function (s) { return [s.label, String(s.count)]; }));

    setChart(charts.scores, data.scores.map(function (s) { return s.label; }), data.scores.map(function (s) { return s.count; }));
    fillTable("table-scores", ["Score", "Leads"], data.scores.map(function (s) { return [s.label, String(s.count)]; }));

    var trend = data.response_times;
    setChart(
      charts.trend,
      trend.map(function (p) { return dayFmt.format(new Date(p.day + "T00:00:00Z")); }),
      trend.map(function (p) { return p.median_seconds === null ? null : +(p.median_seconds / 60).toFixed(2); })
    );
    fillTable("table-trend", ["Day", "Median", "Leads"], trend.map(function (p) {
      return [p.day, duration(p.median_seconds), String(p.responded)];
    }));

    var feed = document.getElementById("feed");
    feed.textContent = "";
    data.activity.forEach(function (a) {
      var li = document.createElement("li");
      var time = document.createElement("time");
      time.dateTime = a.created_at;
      time.textContent = timeFmt.format(new Date(a.created_at));
      var what = document.createElement("span");
      var link = document.createElement("a");
      link.href = "/dashboard/leads/" + encodeURIComponent(a.lead_id);
      link.textContent = a.lead_name;
      what.appendChild(link);
      what.appendChild(document.createTextNode(" · " + a.summary));
      var who = document.createElement("span");
      who.className = "who";
      who.textContent = a.actor;
      li.appendChild(time); li.appendChild(what); li.appendChild(who);
      feed.appendChild(li);
    });

    var updated = document.getElementById("updated");
    updated.textContent = "Updated " + new Date(data.generated_at).toLocaleTimeString();
    updated.parentElement.classList.remove("is-stale");
  }

  function refresh() {
    // X-Background-Refresh: polling is not user activity, so an unattended tab still reaches
    // the session idle timeout (the server then answers 401 and we go to the login page).
    var headers = { Accept: "application/json", "X-Background-Refresh": "1" };
    return fetch(URL_SUMMARY, { credentials: "same-origin", headers: headers })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/login?next=/dashboard"; throw new Error("login"); }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(render)
      .catch(function () {
        var u = document.getElementById("updated");
        u.textContent = "Reconnecting…";
        u.parentElement.classList.add("is-stale");
      });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      if (document.hidden) { schedule(); return; } // no polling in a background tab
      refresh().then(schedule, schedule);
    }, REFRESH_MS);
  }

  makeCharts();
  refresh().then(schedule, schedule);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (mq.addEventListener) mq.addEventListener("change", recolor);
})();
