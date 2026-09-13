// engineering-gap-optimization-plan charts
(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var warn = style.getPropertyValue('--warn').trim();
  var good = style.getPropertyValue('--good').trim();

  // --- Chart 1: priority matrix (benefit x difficulty) ---
  var matrix = echarts.init(document.getElementById('chart-matrix'), null, { renderer: 'svg' });
  matrix.setOption({
    animation: false,
    grid: { left: 46, right: 30, top: 26, bottom: 42 },
    tooltip: {
      appendToBody: true,
      formatter: function (p) {
        return p.data.name + '<br/>收益: ' + p.data.value[1].toFixed(1) + ' · 难度: ' + p.data.value[0].toFixed(1);
      }
    },
    xAxis: {
      name: '投入 / 难度 →', nameLocation: 'middle', nameGap: 26,
      type: 'value', min: 0, max: 5,
      axisLine: { lineStyle: { color: rule } },
      axisLabel: { color: muted }
    },
    yAxis: {
      name: '收益 →', type: 'value', min: 0, max: 5,
      axisLine: { lineStyle: { color: rule } },
      axisLabel: { color: muted }
    },
    series: [{
      type: 'scatter',
      symbolSize: 15,
      data: [
        { value: [1.5, 4.0], name: '1 文件识别', itemStyle: { color: good } },
        { value: [3.0, 4.0], name: '2 解析评测' },
        { value: [1.5, 3.6], name: '5 流程级多跳', itemStyle: { color: accent2 } },
        { value: [3.0, 4.5], name: '4 分层档位', itemStyle: { color: warn } },
        { value: [2.5, 2.5], name: '3 RRF 对照' },
        { value: [2.0, 3.0], name: '6 能力矩阵' }
      ],
      label: { show: true, position: 'right', formatter: function (p) { return p.data.name; }, color: ink, fontSize: 11 },
      markLine: {
        silent: true, symbol: 'none',
        lineStyle: { color: rule, type: 'dashed' },
        data: [{ yAxis: 3.2 }, { xAxis: 2.6 }]
      }
    }]
  });
  window.addEventListener('resize', function () { matrix.resize(); });

  // --- Chart 2: roadmap relative effort ---
  var labels = ['P0 文件识别', 'P1 解析评测', 'P1 流程级多跳', 'P1 分层档位', 'P2 融合对照', 'P2 能力治理'];
  var values = [1, 2.5, 3, 2.5, 1.5, 1.5];
  var effort = echarts.init(document.getElementById('chart-effort'), null, { renderer: 'svg' });
  var colors = [good, accent, accent2, warn, muted, '#7a7f8f'];
  effort.setOption({
    animation: false,
    grid: { left: 8, right: 150, top: 10, bottom: 6, containLabel: true },
    tooltip: { appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', name: '人·周', axisLine: { lineStyle: { color: rule } }, axisLabel: { color: muted } },
    yAxis: {
      type: 'category', inverse: true, data: labels,
      axisLine: { lineStyle: { color: rule } }, axisLabel: { color: ink }
    },
    series: [{
      type: 'bar',
      label: { show: true, position: 'right', formatter: function (p) { return p.value; }, color: muted },
      barWidth: 18,
      data: values.map(function (v, i) { return { value: v, itemStyle: { color: colors[i] } }; })
    }]
  });
  window.addEventListener('resize', function () { effort.resize(); });
})();