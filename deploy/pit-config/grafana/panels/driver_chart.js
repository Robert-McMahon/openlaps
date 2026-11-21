// Driver dashboard: five lap traces stacked on one distance axis.
// Frame A: one row per 10 m of lap distance. Frame B: the track's sections.
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

const TOP = 4;      // % above the first chart, for the legend
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
  grid.push({ left: 64, right: 24, top: top + '%', height: height + '%' });
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
// Start with drag-to-zoom armed, as a Grafana time series behaves.
setTimeout(() => context.panel.chart && context.panel.chart.dispatchAction({
  type: 'takeGlobalCursor', key: 'dataZoomSelect', dataZoomSelectActive: true,
}), 300);
return {
  animation: false,
  legend: {
    top: 0, left: 'center', data: ['Lap', 'Reference'], selectedMode: false,
    textStyle: { color: text }, icon: 'roundRect', itemHeight: 3,
  },
  grid, xAxis, yAxis, series,
  axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: text, type: 'solid' } },
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
          p.marker + (row.series.length > 1 ? p.seriesName + ' ' : '') + '<b>' + Number(p.value[1]).toFixed(row.digits) + '</b>');
        if (cells.length) out.push(row.name + (row.unit ? ' (' + row.unit + ')' : '') + ': ' + cells.join(' &nbsp; '));
      });
      return out.join('<br/>');
    },
  },
  // Drag across a chart to zoom every chart to that stretch of track; the
  // reset button (top right) or the bar at the bottom zooms back out. There
  // is no wheel zoom: a chart this tall must not swallow the page scroll.
  toolbox: {
    right: 20, top: 0, itemSize: 13, iconStyle: { borderColor: text },
    feature: {
      dataZoom: { xAxisIndex: all, yAxisIndex: false, title: { zoom: 'Zoom to a stretch', back: 'Undo zoom' } },
      restore: { title: 'Whole lap' },
    },
  },
  dataZoom: [
    { type: 'slider', xAxisIndex: all, filterMode: 'none', height: 14, bottom: 4, showDetail: false,
      borderColor: line, textStyle: { color: text } },
  ],
};
