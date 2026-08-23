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
    selectedAxis() === "seconds" ? frame / payload.timebase_fps : frame;

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

  // TheSuperHackers @feature Leex 23/08/2026 Keep chart filters bound to one fixed report and derive seconds only from authoritative 30 FPS frames. (#TBD)
  const render = (payload) => {
    if (payload.schema_version !== "web-report-timeline-v1" || payload.timebase_fps !== 30) {
      throw new Error("timeline contract mismatch");
    }
    currentPayload = payload;
    if (!chart) {
      root.style.height = "24rem";
      chart = window.echarts.init(root, null, { renderer: "canvas" });
    }
    const categoricalLabels = Array.from(
      new Set(
        payload.series
          .filter((item) => item.kind === "marker" || item.kind === "band")
          .map((item) => item.label),
      ),
    );
    const seconds = selectedAxis() === "seconds";
    chart.setOption(
      {
        animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
        aria: { enabled: true },
        tooltip: { trigger: "item", renderMode: "richText" },
        legend: { show: true },
        xAxis: { type: "value", name: seconds ? "Seconds (frames / 30)" : "Replay frame" },
        yAxis: [
          { type: "value", name: "Value" },
          { type: "category", data: categoricalLabels, position: "right" },
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
    [...filterInputs, ...axisInputs].forEach((input) => {
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
