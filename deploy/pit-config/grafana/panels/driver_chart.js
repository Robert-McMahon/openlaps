// Driver dashboard: five lap traces stacked on one distance axis, with a map
// of both laps' lines beside them.
// Frame A: one row per 10 m of lap distance. Frame B: the track's sections.
// Frame C: where the car was, every 40 ms of each lap.
const chart = context.panel.chart;
const text = context.grafana.theme.colors.text.secondary;
const line = context.grafana.theme.colors.border.weak;
const frame = (refId) => context.panel.data.series.find((s) => s.refId === refId);
const column = (f, name) => {
  const field = f && f.fields.find((x) => x.name === name);
  return field ? Array.from(field.values) : [];
};

const traces = frame('A');
const distance = column(traces, 'Distance');
if (!distance.length) {
  return { title: { text: 'No samples for these laps', left: 'center', top: 'middle', textStyle: { color: text, fontWeight: 'normal', fontSize: 14 } } };
}
const lapEnd = distance[distance.length - 1] + 10;
const pairs = (name) => {
  const values = column(traces, name);
  return distance.map((d, i) => [d, values[i]]);
};

const marks = frame('B');
const names = column(marks, 'Section');
const kinds = column(marks, 'Kind');
const starts = column(marks, 'From');
const ends = column(marks, 'To');
// A section that spans the start line is drawn as its two ends.
const sections = [];
names.forEach((name, i) => {
  const piece = (from, to) => sections.push({ name, kind: kinds[i], from, to });
  if (starts[i] <= ends[i]) piece(starts[i], ends[i]);
  else { piece(starts[i], lapEnd); piece(0, ends[i]); }
});
sections.sort((a, b) => a.from - b.from);
const sectionAt = (d) => {
  const hit = sections.find((s) => d >= s.from && d < s.to);
  return hit ? hit.name : '';
};

// The map: both laps' positions as metres east and north of their centre.
const where = frame('C');
const which = column(where, 'Which');
const lats = column(where, 'Lat');
const lons = column(where, 'Lon');
const dists = column(where, 'Distance');
const lat0 = lats.reduce((sum, v) => sum + v, 0) / (lats.length || 1);
const lon0 = lons.reduce((sum, v) => sum + v, 0) / (lons.length || 1);
const east = 111320 * Math.cos(lat0 * Math.PI / 180);
const path = (name) => {
  const points = [];
  which.forEach((w, i) => {
    // Distance must only rise, so a position can be looked up by distance.
    if (w !== name || (points.length && dists[i] <= points[points.length - 1].d)) return;
    points.push({ d: dists[i], x: (lons[i] - lon0) * east, y: (lats[i] - lat0) * 110574 });
  });
  return points;
};
const lapPath = path('Lap');
const refPath = path('Reference');
// The map needs room beside the traces; a narrow panel shows the traces alone.
const MAP = 30;     // % of the panel's width
const mapped = lapPath.length > 1 && refPath.length > 1 && chart.getWidth() >= 900;
const indexAt = (points, d) => {
  let lo = 0;
  let hi = points.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid].d <= d) lo = mid; else hi = mid;
  }
  return lo;
};
const positionAt = (points, d) => {
  const i = indexAt(points, d);
  const a = points[i];
  const b = points[Math.min(i + 1, points.length - 1)];
  const t = b.d > a.d ? Math.min(Math.max((d - a.d) / (b.d - a.d), 0), 1) : 0;
  return [a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t];
};
// How far the lap's line is to the left (+) or right (-) of the reference's,
// in metres, as the driver sees it.
const offsetAt = (d) => {
  const [px, py] = positionAt(lapPath, d);
  let best = null;
  const from = indexAt(refPath, d - 80);
  const to = indexAt(refPath, d + 80);
  for (let i = from; i <= to && i < refPath.length - 1; i += 1) {
    const a = refPath[i];
    const b = refPath[i + 1];
    const ux = b.x - a.x;
    const uy = b.y - a.y;
    const length2 = ux * ux + uy * uy;
    if (!length2) continue;
    const t = Math.min(Math.max(((px - a.x) * ux + (py - a.y) * uy) / length2, 0), 1);
    const dx = px - (a.x + ux * t);
    const dy = py - (a.y + uy * t);
    const gap = Math.hypot(dx, dy);
    if (best === null || gap < Math.abs(best)) best = (ux * dy - uy * dx) >= 0 ? gap : -gap;
  }
  return best;
};

const LAP = '#5794F2';
const REF = '#FF9830';
const LOST = '#F2495C';
// name, unit, share of the height, [series: column, label, colour, stepped]
const rows = [
  { name: 'Speed', unit: 'km/h', height: 30, digits: 0, series: [['Speed', 'Lap', LAP], ['Speed ref', 'Reference', REF]] },
  { name: 'Time lost', unit: 's', height: 14, digits: 2, series: [['Time lost', 'Time lost to reference', LOST]] },
  { name: 'Pedal', unit: '%', height: 16, digits: 0, min: 0, max: 100, series: [['Pedal', 'Lap', LAP], ['Pedal ref', 'Reference', REF]] },
  { name: 'Brake', unit: '%', height: 10, digits: 0, min: 0, max: 100, step: true, series: [['Brake', 'Lap', LAP], ['Brake ref', 'Reference', REF]] },
  { name: 'Gear', unit: '', height: 10, digits: 0, min: 0, step: true, series: [['Gear', 'Lap', LAP], ['Gear ref', 'Reference', REF]] },
];

const TOP = 7;      // % above the first chart, for the legend and section names
const GAP = 2.5;    // % between charts
const BOTTOM = 11;  // % below the last chart, for section names and the zoom bar
const scale = (100 - TOP - BOTTOM - GAP * (rows.length - 1)) / rows.reduce((sum, r) => sum + r.height, 0);
let top = TOP;
const grid = [];
const xAxis = [];
const yAxis = [];
const series = [];
const last = rows.length - 1;
const mids = sections.map((s) => (s.from + s.to) / 2);
const edges = sections.map((s) => s.from).filter((d) => d > 0);

rows.forEach((row, i) => {
  const height = row.height * scale;
  grid.push({ left: 64, right: mapped ? MAP + 2 + '%' : 24, top: top + '%', height: height + '%' });
  top += height + GAP;
  xAxis.push({
    gridIndex: i, type: 'value', min: 0, max: lapEnd,
    axisLine: { lineStyle: { color: line } },
    axisTick: { show: false },
    // Corner and straight names instead of metres, on the bottom chart only.
    axisLabel: i === last && mids.length
      ? { show: true, color: text, fontSize: 11, hideOverlap: true, customValues: mids, formatter: (v) => sectionAt(v) }
      : { show: i === last && !mids.length, color: text },
    splitLine: { show: false },
    axisPointer: { label: { show: i === last, formatter: (p) => Math.round(p.value) + ' m  ' + sectionAt(p.value) } },
  });
  yAxis.push({
    gridIndex: i, type: 'value', name: row.name + (row.unit ? ' (' + row.unit + ')' : ''),
    nameLocation: 'middle', nameGap: 44, nameTextStyle: { color: text, fontSize: 11 },
    min: row.min, max: row.max, scale: row.min === undefined,
    minInterval: row.name === 'Gear' ? 1 : undefined,
    axisLabel: { color: text, fontSize: 11 },
    splitLine: { lineStyle: { color: line, opacity: 0.5 } }, splitNumber: 3,
  });
  row.series.forEach(([columnName, label, colour], n) => {
    const item = {
      name: label, type: 'line', xAxisIndex: i, yAxisIndex: i, showSymbol: false,
      step: row.step ? 'end' : false, connectNulls: true,
      lineStyle: { width: 1.6, color: colour }, itemStyle: { color: colour },
      emphasis: { disabled: true }, data: pairs(columnName),
    };
    if (n === 0 && sections.length) {
      // Corners shaded, every section edge ruled, in each chart.
      item.markArea = {
        silent: true, itemStyle: { color: 'rgba(128, 128, 128, 0.10)' }, label: { show: false },
        data: sections.filter((s) => s.kind === 'corner').map((s) => [{ xAxis: s.from }, { xAxis: s.to }]),
      };
      item.markLine = {
        silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: line, type: 'solid', width: 1 },
        data: edges.map((d) => ({ xAxis: d })),
      };
    }
    series.push(item);
  });
});

const all = rows.map((_, i) => i);

// The map is one more chart to the right of the traces. Its two axes are in
// metres and must have the same scale, so their limits are worked out from
// the size of the panel: `fit` returns limits that show the stretch of track
// between two lap distances.
const fit = (from, to) => {
  const shown = lapPath.concat(refPath).filter((p) => p.d >= from && p.d <= to);
  const points = shown.length > 1 ? shown : lapPath.concat(refPath);
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const width = chart.getWidth() * MAP / 100 - 16;
  const height = chart.getHeight() * (100 - TOP - BOTTOM) / 100;
  const spanX = Math.max(Math.max(...xs) - Math.min(...xs), 40);
  const spanY = Math.max(Math.max(...ys) - Math.min(...ys), 40);
  const perPixel = Math.max(spanX / width, spanY / height) * 1.12;
  const midX = (Math.max(...xs) + Math.min(...xs)) / 2;
  const midY = (Math.max(...ys) + Math.min(...ys)) / 2;
  return {
    x: { min: midX - perPixel * width / 2, max: midX + perPixel * width / 2 },
    y: { min: midY - perPixel * height / 2, max: midY + perPixel * height / 2 },
  };
};
const m = rows.length;
if (mapped) {
  const whole = fit(0, lapEnd);
  const hidden = { show: false, axisPointer: { show: false, triggerTooltip: false } };
  grid.push({ left: 100 - MAP + '%', right: 16, top: TOP + '%', bottom: BOTTOM + '%' });
  xAxis.push({ id: 'map-x', gridIndex: m, type: 'value', ...hidden, ...whole.x });
  yAxis.push({ id: 'map-y', gridIndex: m, type: 'value', ...hidden, ...whole.y });
  const trace = (id, name, colour, points) => ({
    id, name, type: 'line', xAxisIndex: m, yAxisIndex: m, showSymbol: false, silent: true,
    lineStyle: { width: 1.6, color: colour }, itemStyle: { color: colour },
    emphasis: { disabled: true }, tooltip: { show: false }, data: points.map((p) => [p.x, p.y]),
  });
  const dot = (id, colour) => ({
    id, type: 'scatter', xAxisIndex: m, yAxisIndex: m, silent: true, symbolSize: 11, z: 5,
    itemStyle: { color: colour, borderColor: context.grafana.theme.colors.text.primary, borderWidth: 1.5 },
    tooltip: { show: false }, data: [],
  });
  series.push(trace('map-ref', 'Reference', REF, refPath), trace('map-lap', 'Lap', LAP, lapPath));
  // Corner names, each beside the middle of its corner.
  series.push({
    id: 'map-names', type: 'scatter', xAxisIndex: m, yAxisIndex: m, silent: true, symbolSize: 1,
    itemStyle: { color: 'transparent' }, tooltip: { show: false },
    label: { show: true, formatter: (p) => p.data.name, color: text, fontSize: 10, position: 'right', distance: 8 },
    labelLayout: { hideOverlap: true },
    data: sections.filter((s) => s.kind === 'corner')
      .map((s) => ({ name: s.name, value: positionAt(refPath, (s.from + s.to) / 2) })),
  });
  series.push(dot('map-dot-ref', REF), dot('map-dot-lap', LAP));
}

// The map follows the traces: a dot on each line at the crosshair's distance,
// and the same stretch of track when the traces are zoomed. Pointing at the
// map moves the crosshair to that place on the lap.
const listeners = [];
const listen = (target, event, handler) => { target.on(event, handler); listeners.push(() => target.off(event, handler)); };
if (mapped) {
  let shownAt = null;
  const dots = (d) => {
    if (d === shownAt) return;
    shownAt = d;
    chart.setOption({ series: [
      { id: 'map-dot-ref', data: d === null ? [] : [positionAt(refPath, d)] },
      { id: 'map-dot-lap', data: d === null ? [] : [positionAt(lapPath, d)] },
    ] });
  };
  listen(chart, 'updateAxisPointer', (event) => {
    const axis = (event.axesInfo || []).find((a) => a.axisDim === 'x' && a.axisIndex < m);
    dots(axis ? axis.value : null);
  });
  const stretch = () => {
    const zoom = (chart.getOption().dataZoom || [])[0] || {};
    return [zoom.startValue == null ? 0 : zoom.startValue, zoom.endValue == null ? lapEnd : zoom.endValue];
  };
  const refit = () => {
    const limits = fit(...stretch());
    chart.setOption({ xAxis: [{ id: 'map-x', ...limits.x }], yAxis: [{ id: 'map-y', ...limits.y }] });
  };
  listen(chart, 'datazoom', refit);
  listen(chart, 'restore', () => { shownAt = null; });
  const resized = new ResizeObserver(() => requestAnimationFrame(() => !chart.isDisposed() && refit()));
  resized.observe(chart.getDom());
  listeners.push(() => resized.disconnect());
  let onMap = false;
  listen(chart.getZr(), 'mousemove', (event) => {
    const at = [event.offsetX, event.offsetY];
    if (!chart.containPixel({ gridIndex: m }, at)) {
      if (onMap) chart.dispatchAction({ type: 'hideTip' });
      onMap = false;
      return;
    }
    onMap = true;
    const [x, y] = chart.convertFromPixel({ xAxisIndex: m, yAxisIndex: m }, at);
    const [from, to] = stretch();
    let nearest = null;
    let gap = Infinity;
    lapPath.forEach((p) => {
      const g = (p.x - x) * (p.x - x) + (p.y - y) * (p.y - y);
      if (g < gap && p.d >= from && p.d <= to) { gap = g; nearest = p; }
    });
    if (!nearest) return;
    const speed = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [nearest.d, 0]);
    // After the chart's own handling of this mouse move, which hides the readout.
    setTimeout(() => onMap && !chart.isDisposed() && chart.dispatchAction({
      type: 'showTip', x: speed[0], y: chart.getHeight() * (TOP + 2) / 100,
    }), 0);
  });
}
// Start with drag-to-zoom armed, as a Grafana time series behaves.
setTimeout(() => context.panel.chart && context.panel.chart.dispatchAction({
  type: 'takeGlobalCursor', key: 'dataZoomSelect', dataZoomSelectActive: true,
}), 300);
// The section names again along the top of the first chart: one more axis on
// that chart, which zooms and takes the crosshair with the others.
const along = all.slice();
if (mids.length) {
  along.push(xAxis.length);
  xAxis.push({
    gridIndex: 0, type: 'value', position: 'top', min: 0, max: lapEnd,
    axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false },
    axisLabel: { show: true, color: text, fontSize: 11, hideOverlap: true, customValues: mids, formatter: (v) => sectionAt(v) },
    axisPointer: { label: { show: false } },
  });
}
const option = {
  animation: false,
  legend: {
    top: 0, left: 'center', data: ['Lap', 'Reference'], selectedMode: false,
    textStyle: { color: text }, icon: 'roundRect', itemHeight: 3,
  },
  grid, xAxis, yAxis, series,
  axisPointer: { link: [{ xAxisIndex: along }], lineStyle: { color: text, type: 'solid' } },
  tooltip: {
    trigger: 'axis', confine: true, transitionDuration: 0,
    backgroundColor: context.grafana.theme.colors.background.secondary,
    borderColor: line, textStyle: { color: context.grafana.theme.colors.text.primary, fontSize: 12 },
    formatter: (params) => {
      if (!params.length) return '';
      const d = params[0].axisValue;
      const out = ['<b>' + Math.round(d) + ' m' + (sectionAt(d) ? ' &nbsp; ' + sectionAt(d) : '') + '</b>'];
      rows.forEach((row, i) => {
        const cells = params.filter((p) => p.axisIndex === i && p.value[1] != null).map((p) =>
          p.marker + '<b>' + Number(p.value[1]).toFixed(row.digits) + '</b>');
        if (cells.length) out.push(row.name + (row.unit ? ' (' + row.unit + ')' : '') + ': ' + cells.join(' &nbsp; '));
      });
      const offset = mapped ? offsetAt(d) : null;
      if (offset !== null) {
        out.push('Line: ' + (Math.abs(offset) < 0.05 ? 'on the reference'
          : '<b>' + Math.abs(offset).toFixed(1) + ' m</b> ' + (offset > 0 ? 'left' : 'right') + ' of the reference'));
      }
      return out.join('<br/>');
    },
  },
  // Drag across a chart to zoom every chart to that stretch of track; the
  // reset button (top right) or the bar at the bottom zooms back out. There
  // is no wheel zoom: a chart this tall must not swallow the page scroll.
  toolbox: {
    right: mapped ? MAP + 2 + '%' : 20, top: 0, itemSize: 13, iconStyle: { borderColor: text },
    feature: {
      dataZoom: { xAxisIndex: along, yAxisIndex: false, title: { zoom: 'Zoom to a stretch', back: 'Undo zoom' } },
      restore: { title: 'Whole lap' },
    },
  },
  dataZoom: [
    { type: 'slider', xAxisIndex: along, filterMode: 'none', height: 14, bottom: 4, showDetail: false,
      left: 64, right: mapped ? MAP + 2 + '%' : 24,
      borderColor: line, textStyle: { color: text } },
  ],
};
// The panel calls `unsubscribe` before it runs this code again.
return { version: 2, option, unsubscribe: () => listeners.forEach((stop) => stop()) };
