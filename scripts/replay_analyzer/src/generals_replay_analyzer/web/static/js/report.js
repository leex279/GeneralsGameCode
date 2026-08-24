(() => {
  "use strict";

  const root = document.querySelector("[data-report-timeline]");
  const controls = document.querySelector("[data-timeline-controls]");
  if (!(root instanceof HTMLElement) || !window.echarts) {
    return;
  }

  const source = root.dataset.source;
  if (!source) {
    return;
  }

  const filterInputs = Array.from(
    document.querySelectorAll("[data-timeline-player], [data-timeline-family]"),
  );
  const axisInputs = Array.from(document.querySelectorAll("[data-timeline-axis]"));
  let chart = null;
  let currentPayload = null;

  const checkedValues = (selector) =>
    Array.from(document.querySelectorAll(`${selector}:checked`), (input) => input.value);

  const selectedAxis = () =>
    document.querySelector("[data-timeline-axis]:checked")?.value === "seconds" ? "seconds" : "frame";

  const axisValue = (frame, payload) =>
    selectedAxis() === "seconds" && payload.timebase_fps !== null
      ? frame / payload.timebase_fps
      : frame;

  const synchronizeAxisControls = (payload) => {
    axisInputs.forEach((input) => {
      const authorityUnavailable =
        input.value === "seconds" && payload.timebase_fps === null;
      input.disabled = authorityUnavailable;
      if (authorityUnavailable && input.checked) {
        const frameInput = axisInputs.find((candidate) => candidate.value === "frame");
        if (frameInput) {
          frameInput.checked = true;
        }
      }
    });
  };

  const chartSeries = (payload) =>
    payload.series
      .filter((item) => item.availability.state !== "unavailable")
      .map((item, seriesIndex) => {
        if (item.kind === "band") {
          return {
            id: item.series_id,
            name: item.label,
            type: "custom",
            yAxisIndex: 1,
            encode: { x: [0, 1], y: 2 },
            data: item.intervals.map((interval) => [
              axisValue(interval.frame_start, payload),
              axisValue(interval.frame_end, payload),
              item.label,
              interval.label,
            ]),
            renderItem(_params, api) {
              const start = api.coord([api.value(0), api.value(2)]);
              const end = api.coord([api.value(1), api.value(2)]);
              const height = Math.max(8, api.size([0, 1])[1] * 0.55);
              return {
                type: "rect",
                shape: {
                  x: start[0],
                  y: start[1] - height / 2,
                  width: Math.max(1, end[0] - start[0]),
                  height,
                },
                style: api.style(),
              };
            },
          };
        }
        if (item.kind === "marker") {
          return {
            id: item.series_id,
            name: item.label,
            type: "scatter",
            yAxisIndex: 1,
            symbolSize: 10,
            data: item.points.map((point) => [
              axisValue(point.frame, payload),
              item.label,
              point.label,
              point.value,
            ]),
          };
        }
        return {
          id: item.series_id,
          name: item.label,
          type: "line",
          yAxisIndex: 0,
          step: item.kind === "step" ? "end" : false,
          showSymbol: true,
          data: item.points.map((point) => [
            axisValue(point.frame, payload),
            typeof point.value === "number" ? point.value : seriesIndex,
            point.label,
            point.value,
          ]),
        };
      });

  // TheSuperHackers @feature Leex 24/08/2026 Keep chart filters bound to one fixed report and derive seconds only from the replay-specific authoritative clock. (#TBD)
  const render = (payload) => {
    if (payload.schema_version !== "web-report-timeline-v1" || (payload.timebase_fps !== null && payload.timebase_fps !== 30 && payload.timebase_fps !== 60)) {
      throw new Error("timeline contract mismatch");
    }
    currentPayload = payload;
    synchronizeAxisControls(payload);
    if (!chart) {
      // TheSuperHackers @fix Leex 23/08/2026 Keep the evidence timeline compact beneath its explicit player and family filters. (#TBD)
      root.style.height = "12rem";
      chart = window.echarts.init(root, null, { renderer: "canvas" });
    }
    const categoricalLabels = Array.from(
      new Set(
        payload.series
          .filter((item) => item.kind === "marker" || item.kind === "band")
          .map((item) => item.label),
      ),
    );
    const seconds = selectedAxis() === "seconds" && payload.timebase_fps !== null;
    chart.setOption(
      {
        animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
        aria: { enabled: true },
        backgroundColor: "transparent",
        color: ["#7fc6f5", "#a9d05a", "#f0966e", "#4a9fd8", "#bfe3ff"],
        textStyle: { color: "#9bb4c4", fontFamily: "Cascadia Mono, Consolas, monospace" },
        tooltip: {
          trigger: "item",
          renderMode: "richText",
          backgroundColor: "#0d1a24",
          borderColor: "#31536a",
          textStyle: { color: "#eaf6ff" },
        },
        legend: { show: false, textStyle: { color: "#9bb4c4" } },
        xAxis: {
          type: "value",
          name: seconds ? `Seconds (frames / ${payload.timebase_fps})` : "Replay frame",
          axisLine: { lineStyle: { color: "#31536a" } },
          splitLine: { lineStyle: { color: "#1a2f41" } },
        },
        yAxis: [
          {
            type: "value",
            name: "Value",
            axisLine: { lineStyle: { color: "#31536a" } },
            splitLine: { lineStyle: { color: "#1a2f41" } },
          },
          {
            type: "category",
            data: categoricalLabels,
            position: "right",
            axisLine: { lineStyle: { color: "#31536a" } },
            axisLabel: { show: false },
            axisTick: { show: false },
          },
        ],
        series: chartSeries(payload),
      },
      { notMerge: true },
    );
    // TheSuperHackers @fix Leex 23/08/2026 Restore the authored accessible name after ECharts rewrites ARIA attributes. (#TBD)
    root.setAttribute("role", "img");
    root.setAttribute("aria-label", "Replay event timeline");
  };

  const filteredSource = () => {
    const url = new URL(source, window.location.origin);
    checkedValues("[data-timeline-player]").forEach((value) => url.searchParams.append("player", value));
    checkedValues("[data-timeline-family]").forEach((value) => url.searchParams.append("family", value));
    return url;
  };

  const showFallback = () => {
    root.hidden = true;
    const feedback = document.querySelector("#app-feedback");
    if (feedback) {
      feedback.textContent = "Timeline chart unavailable; use the frame table.";
    }
  };

  const load = () => {
    root.hidden = false;
    return fetch(filteredSource(), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error("timeline unavailable");
        }
        return response.json();
      })
      .then(render)
      .catch(showFallback);
  };

  const preserveOneSelection = (event, selector) => {
    const group = Array.from(document.querySelectorAll(selector));
    if (group.length > 0 && !group.some((input) => input.checked)) {
      event.currentTarget.checked = true;
      return false;
    }
    return true;
  };

  if (controls) {
    filterInputs.forEach((input) => {
      input.disabled = false;
    });
    filterInputs.forEach((input) => {
      input.addEventListener("change", (event) => {
        const selector = input.matches("[data-timeline-player]")
          ? "[data-timeline-player]"
          : "[data-timeline-family]";
        if (preserveOneSelection(event, selector)) {
          load();
        }
      });
    });
    axisInputs.forEach((input) => {
      input.addEventListener("change", () => {
        if (currentPayload) {
          render(currentPayload);
        }
      });
    });
  }
  window.addEventListener("resize", () => chart?.resize());
  load();
})();
