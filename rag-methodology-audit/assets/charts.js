(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var passC = style.getPropertyValue('--pass').trim();
  var partialC = style.getPropertyValue('--partial').trim();
  var failC = style.getPropertyValue('--fail').trim();

  // --- Chart 1: Radar (three dimensions) ---
  var radarEl = document.getElementById('chart-radar');
  if (radarEl) {
    var radar = echarts.init(radarEl, null, { renderer: 'svg' });
    radar.setOption({
      animation: false,
      tooltip: { appendToBody: true },
      legend: {
        data: ['符合度 (%)'],
        bottom: 0,
        textStyle: { color: muted, fontSize: 12 }
      },
      radar: {
        indicator: [
          { name: '问题边界与路由', max: 100 },
          { name: '三张面职责', max: 100 },
          { name: '五道上线闸门', max: 100 }
        ],
        center: ['50%', '52%'],
        radius: '62%',
        axisName: { color: ink, fontSize: 13, fontWeight: 600 },
        splitLine: { lineStyle: { color: rule } },
        splitArea: { areaStyle: { color: [bg2, '#f0f4f7'] } },
        axisLine: { lineStyle: { color: rule } }
      },
      series: [{
        type: 'radar',
        data: [{
          value: [69, 50, 64],
          name: '符合度 (%)',
          areaStyle: { color: accent + '33' },
          lineStyle: { color: accent, width: 2 },
          itemStyle: { color: accent },
          label: { show: true, color: ink, fontWeight: 700, formatter: '{c}%' }
        }]
      }]
    });
    window.addEventListener('resize', function() { radar.resize(); });
  }

  // helper: horizontal bar with status colors
  function makeBar(elId, categories, values, title) {
    var el = document.getElementById(elId);
    if (!el) return null;
    var chart = echarts.init(el, null, { renderer: 'svg' });
    var colors = values.map(function(v) {
      if (v >= 80) return passC;
      if (v >= 50) return partialC;
      return failC;
    });
    chart.setOption({
      animation: false,
      title: { text: title, left: 'center', textStyle: { color: muted, fontSize: 12, fontWeight: 400 } },
      tooltip: {
        appendToBody: true,
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function(p) { return p[0].name + '：' + p[0].value + '%'; }
      },
      grid: { left: '3%', right: '8%', bottom: '3%', top: '12%', containLabel: true },
      xAxis: {
        type: 'value', max: 100,
        axisLabel: { color: muted, fontSize: 11, formatter: '{value}%' },
        axisLine: { lineStyle: { color: rule } },
        splitLine: { lineStyle: { color: rule } }
      },
      yAxis: {
        type: 'category',
        data: categories,
        axisLabel: { color: ink, fontSize: 12 },
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false }
      },
      series: [{
        type: 'bar',
        data: values.map(function(v, i) {
          return { value: v, itemStyle: { color: colors[i], borderRadius: [0, 4, 4, 0] } };
        }),
        barWidth: '55%',
        label: {
          show: true, position: 'right',
          color: ink, fontWeight: 700, fontSize: 12,
          formatter: '{c}%'
        }
      }]
    });
    return chart;
  }

  // --- Chart 2: Dim1 sub-items ---
  var c2 = makeBar('chart-dim1',
    ['路由表五路径', '终态意图', '按需检索', '证据门禁', 'Claim核验', '拒答机制', '非功能约束', '六个需求问题'],
    [60, 100, 60, 50, 50, 85, 80, 80],
    '已实现=满分 · 部分实现=50-80 · 未达标<50'
  );
  if (c2) window.addEventListener('resize', function() { c2.resize(); });

  // --- Chart 3: Dim2 sub-items ---
  var c3 = makeBar('chart-dim2',
    ['数据面', '查询面', '控制面', '交接契约', '端到端Trace', '冲突处理'],
    [45, 95, 55, 50, 55, 25],
    '已实现=满分 · 部分实现=50-80 · 未达标<50'
  );
  if (c3) window.addEventListener('resize', function() { c3.resize(); });

  // --- Chart 4: Dim3 five gates ---
  var c4 = makeBar('chart-dim3',
    ['闸门1 数据完整性', '闸门2 权限隔离', '闸门3 检索证据', '闸门4 故障恢复', '闸门5 资源预算'],
    [40, 95, 95, 55, 50],
    '已实现=满分 · 部分实现=50-80 · 未达标<50'
  );
  if (c4) window.addEventListener('resize', function() { c4.resize(); });

})();
