(() => {
  "use strict";

  const chartNode = document.querySelector("[data-map-chart]");
  const rasterSelect = document.querySelector("[data-map-raster-select]");
  const statusNode = document.getElementById("map-chart-status");

  const frameStart = document.getElementById("frame-start");
  const frameEnd = document.getElementById("frame-end");
  const frameStartSlider = document.getElementById("frame-start-slider");
  const frameEndSlider = document.getElementById("frame-end-slider");
  if (frameStart instanceof HTMLInputElement
      && frameEnd instanceof HTMLInputElement
      && frameStartSlider instanceof HTMLInputElement
      && frameEndSlider instanceof HTMLInputElement) {
    const updateWindow = (source, isStart) => {
      const value = source.value;
      if (isStart) {
        frameStart.value = value;
        frameStartSlider.value = value;
        if (Number(value) > Number(frameEnd.value)) {
          frameEnd.value = value;
          frameEndSlider.value = value;
        }
      } else {
        frameEnd.value = value;
        frameEndSlider.value = value;
        if (Number(value) < Number(frameStart.value)) {
          frameStart.value = value;
          frameStartSlider.value = value;
        }
      }
    };
    frameStart.addEventListener("input", () => updateWindow(frameStart, true));
    frameEnd.addEventListener("input", () => updateWindow(frameEnd, false));
    frameStartSlider.addEventListener("input", () => updateWindow(frameStartSlider, true));
    frameEndSlider.addEventListener("input", () => updateWindow(frameEndSlider, false));
  }

  if (!(chartNode instanceof HTMLElement)
      || !(rasterSelect instanceof HTMLSelectElement)
      || !(statusNode instanceof HTMLElement)) {
    return;
  }
  if (!window.echarts) {
    statusNode.textContent = "Interactive chart unavailable because the chart library did not load. The evidence tables remain available.";
    return;
  }

  const sceneUrl = chartNode.dataset.mapSceneUrl;
  if (!sceneUrl || !sceneUrl.startsWith("/api/replays/")) {
    statusNode.textContent = "The canonical scene URL is unavailable.";
    return;
  }

  const chart = window.echarts.init(chartNode);
  window.addEventListener("resize", () => chart.resize());

  const suppliedPosition = (item, display) => {
    if (!item || !item.position) return null;
    if (display === "map_normalized") {
      return item.position.map_normalized
        ? [item.position.map_normalized.u, item.position.map_normalized.v]
        : null;
    }
    if (display === "player_centric") {
      return item.position.player_centric
        ? [item.position.player_centric.forward, item.position.player_centric.left]
        : null;
    }
    return item.position.raw ? [item.position.raw.x, item.position.raw.y] : null;
  };

  const pointPosition = (point, display) => suppliedPosition({position: point}, display);
  const present = (values) => values.filter((value) => Array.isArray(value));

  const rasterSeries = (display) => {
    const option = rasterSelect.selectedOptions[0];
    if (display !== "raw" || !option || !option.value.startsWith("/api/maps/")) return [];
    const rawMinimumX = Number(option.dataset.rawMinimumX);
    const rawMinimumY = Number(option.dataset.rawMinimumY);
    const rawMaximumX = Number(option.dataset.rawMaximumX);
    const rawMaximumY = Number(option.dataset.rawMaximumY);
    if (![rawMinimumX, rawMinimumY, rawMaximumX, rawMaximumY].every(Number.isFinite)) return [];
    return [{
      name: "Authoritative raster",
      type: "custom",
      silent: true,
      data: [[rawMinimumX, rawMinimumY, rawMaximumX, rawMaximumY]],
      renderItem: (_params, api) => {
        const topLeft = api.coord([rawMinimumX, rawMaximumY]);
        const bottomRight = api.coord([rawMaximumX, rawMinimumY]);
        return {
          type: "image",
          style: {
            image: option.value,
            opacity: 0.55,
            x: topLeft[0],
            y: topLeft[1],
            width: bottomRight[0] - topLeft[0],
            height: bottomRight[1] - topLeft[1],
          },
        };
      },
      z: -10,
    }];
  };

  const axisOptions = (scene, display) => {
    if (display === "map_normalized") {
      return {
        xAxis: {type: "value", name: "Map normalized U", min: 0, max: 1},
        yAxis: {type: "value", name: "Map normalized V", min: 0, max: 1},
      };
    }
    if (display === "player_centric") {
      return {
        xAxis: {type: "value", name: "Player-centric forward"},
        yAxis: {type: "value", name: "Player-centric left"},
      };
    }
    const option = rasterSelect.selectedOptions[0];
    const rawMinimumX = option ? Number(option.dataset.rawMinimumX) : scene.transforms.raw.minimum.x;
    const rawMinimumY = option ? Number(option.dataset.rawMinimumY) : scene.transforms.raw.minimum.y;
    const rawMaximumX = option ? Number(option.dataset.rawMaximumX) : scene.transforms.raw.maximum.x;
    const rawMaximumY = option ? Number(option.dataset.rawMaximumY) : scene.transforms.raw.maximum.y;
    return {
      xAxis: {type: "value", name: "Raw world X", min: rawMinimumX, max: rawMaximumX},
      yAxis: {type: "value", name: "Raw world Y", min: rawMinimumY, max: rawMaximumY},
    };
  };

  const render = (scene) => {
    if (scene.schema_version !== "replay-map-scene-v1"
        || scene.query.report_public_id !== scene.report_public_id) {
      throw new Error("Unexpected map scene schema");
    }
    const display = scene.query.coordinate_display;
    const routes = scene.routes
      .filter((route) => route.availability.state === "available")
      .map((route) => ({
        name: `Validated ${route.locomotor_surface} route`,
        type: "line",
        showSymbol: false,
        data: present(route.points.map((point) => pointPosition(point, display))),
      }));
    const orderTargets = scene.orders
      .filter((order) => order.target_position)
      .map((order) => suppliedPosition({position: order.target_position}, display));
    const series = [
      ...rasterSeries(display),
      {name: "Starts", type: "scatter", symbol: "rect", data: present(scene.starts.map((item) => suppliedPosition(item, display)))},
      {name: "Resources", type: "scatter", symbol: "pin", data: present(scene.resources.map((item) => suppliedPosition(item, display)))},
      {name: "Structures", type: "scatter", symbol: "roundRect", data: present(scene.structures.map((item) => suppliedPosition(item, display)))},
      {name: "Observed samples", type: "scatter", symbol: "circle", data: present(scene.samples.map((item) => suppliedPosition(item, display)))},
      {name: "Order targets", type: "scatter", symbol: "diamond", data: present(orderTargets)},
      ...routes,
      {name: "Engagements", type: "scatter", symbol: "diamond", data: present(scene.engagements.map((item) => suppliedPosition({position: item.centroid}, display)))},
      {name: "Casualties", type: "scatter", symbol: "triangle", data: present(scene.casualties.map((item) => suppliedPosition(item, display)))},
    ];
    chart.clear();
    chart.setOption({
      animation: false,
      aria: {enabled: true, description: "Accepted spatial facts in the selected supplied coordinate display."},
      backgroundColor: "transparent",
      color: ["#7fc6f5", "#a9d05a", "#f0966e", "#4a9fd8", "#bfe3ff"],
      textStyle: {color: "#9bb4c4", fontFamily: "Cascadia Mono, Consolas, monospace"},
      ...axisOptions(scene, display),
      tooltip: {trigger: "item", backgroundColor: "#0d1a24", borderColor: "#31536a", textStyle: {color: "#eaf6ff"}},
      legend: {type: "scroll", textStyle: {color: "#9bb4c4"}},
      series,
    });
    // TheSuperHackers @fix Leex 23/08/2026 Restore the authored map name after ECharts rewrites ARIA attributes. (#TBD)
    chartNode.setAttribute("role", "img");
    chartNode.setAttribute("aria-label", "Authoritative replay map scene");
    const reasons = scene.availability.reason_codes || [];
    const presenceNote = scene.control_windows.length === 0
      ? " Presence cells are unavailable and are not inferred."
      : " Presence facts remain in the evidence table because cells have no supplied display position.";
    statusNode.textContent = `Rendered ${display} coordinates.${presenceNote}`
      + (reasons.length ? ` Unavailable evidence: ${reasons.join(", ")}.` : "");
  };

  let fixedScene = null;
  rasterSelect.addEventListener("change", () => {
    if (fixedScene) render(fixedScene);
  });
  fetch(sceneUrl, {headers: {Accept: "application/json"}, credentials: "same-origin"})
    .then((response) => {
      if (!response.ok) throw new Error("Map scene unavailable");
      return response.json();
    })
    .then((scene) => {
      fixedScene = scene;
      render(scene);
    })
    .catch(() => {
      statusNode.textContent = "The interactive scene could not be loaded. The evidence tables remain available.";
    });
})();
