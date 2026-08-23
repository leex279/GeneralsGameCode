(() => {
  "use strict";

  const allowedKinds = new Set(["players", "matches", "openings", "strategies", "time_periods"]);
  const allowedMetricKinds = new Set(["scalar", "distribution", "categorical_share", "timing_band", "transition", "trend"]);
  const chartPalette = ["#7fc6f5", "#f0966e", "#a9d05a", "#4a9fd8"];
  const axisStyle = {axisLine: {lineStyle: {color: "#31536a"}}, splitLine: {lineStyle: {color: "#1a2f41"}}};

  const announce = (message) => {
    const region = document.querySelector("#app-feedback");
    if (region) region.textContent = message;
  };

  const fetchFixed = async (element) => {
    const source = element.dataset.source;
    if (!source) return null;
    const url = new URL(source, window.location.origin);
    if (url.origin !== window.location.origin || url.protocol !== window.location.protocol) return null;
    const response = await fetch(url, {headers: {Accept: "application/json"}, credentials: "same-origin"});
    if (!response.ok) throw new Error("fixed comparison data unavailable");
    return response.json();
  };

  const renderComparison = async (element) => {
    const payload = await fetchFixed(element);
    if (!payload || payload.version?.schema_version !== "replay-comparison-v1" || !allowedKinds.has(payload.query?.kind)) return;
    const comparable = payload.metrics.filter((metric) =>
      allowedMetricKinds.has(metric.value_kind) &&
      ["comparable", "partial"].includes(metric.state) &&
      typeof metric.left?.raw_value === "number" &&
      typeof metric.right?.raw_value === "number"
    );
    if (!comparable.length || !window.echarts) return;
    // TheSuperHackers @fix Leex 23/08/2026 Give renderable comparison charts a stable visible canvas height. (#TBD)
    element.style.height = "24rem";
    const chart = window.echarts.init(element, null, {renderer: "canvas"});
    chart.setOption({
      animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      aria: {enabled: true},
      backgroundColor: "transparent",
      color: chartPalette,
      textStyle: {color: "#9bb4c4", fontFamily: "Cascadia Mono, Consolas, monospace"},
      tooltip: {show: false},
      xAxis: {type: "category", data: comparable.map((metric) => metric.label), ...axisStyle},
      yAxis: {type: "value", ...axisStyle},
      series: [
        {name: "Left", type: "bar", data: comparable.map((metric) => metric.left.raw_value)},
        {name: "Right", type: "bar", data: comparable.map((metric) => metric.right.raw_value)},
      ],
    });
    // TheSuperHackers @fix Leex 23/08/2026 Preserve authored chart names after ECharts initializes its canvas. (#TBD)
    element.setAttribute("role", "img");
    element.setAttribute("aria-label", "Comparison chart");
  };

  const renderProfile = async (element) => {
    const payload = await fetchFixed(element);
    if (!payload || payload.version?.schema_version !== "replay-player-profile-v1" || !window.echarts) return;
    const values = payload.insights.filter((insight) => typeof insight.raw_value === "number" && insight.availability?.state !== "unavailable");
    if (!values.length) return;
    element.style.height = "24rem";
    const chart = window.echarts.init(element, null, {renderer: "canvas"});
    chart.setOption({
      animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      aria: {enabled: true},
      backgroundColor: "transparent",
      color: chartPalette,
      textStyle: {color: "#9bb4c4", fontFamily: "Cascadia Mono, Consolas, monospace"},
      tooltip: {show: false},
      xAxis: {type: "category", data: values.map((insight) => insight.label), ...axisStyle},
      yAxis: {type: "value", ...axisStyle},
      series: [{name: "Raw Value", type: "bar", data: values.map((insight) => insight.raw_value)}],
    });
    element.setAttribute("role", "img");
    element.setAttribute("aria-label", "Player distribution chart");
  };

  const tasks = [
    ...Array.from(document.querySelectorAll("[data-comparison-chart]"), renderComparison),
    ...Array.from(document.querySelectorAll("[data-player-profile-chart]"), renderProfile),
  ];
  Promise.all(tasks).catch(() => announce("Optional chart unavailable. The evidence tables remain complete."));
})();
