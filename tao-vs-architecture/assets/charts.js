// TAO vs 项目架构 — 覆盖度评分图
(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var green = style.getPropertyValue('--green').trim();
  var amber = style.getPropertyValue('--amber').trim();
  var red = style.getPropertyValue('--red').trim();

  // --- Chart: TAO 维度覆盖度 ---
  var el = document.getElementById('chart-coverage');
  if (!el) return;

  var chart = echarts.init(el, null, { renderer: 'svg' });

  var dims = [
    { name: 'Control State 控制状态', score: 90, color: green },
    { name: 'Fact/Evidence 证据绑定', score: 85, color: green },
    { name: '评估体系', score: 85, color: green },
    { name: 'Action State 动作状态', score: 60, color: amber },
    { name: '6 个合法出口', score: 55, color: amber },
    { name: 'Observation 观察', score: 50, color: amber },
    { name: 'Goal State 目标状态', score: 45, color: amber },
    { name: '双层循环 外层监督', score: 30, color: red }
  ];

  chart.setOption({
    animation: false,
    grid: { left: 10, right: 40, top: 10, bottom: 10, containLabel: true },
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      axisPointer: { type: 'shadow' },
      formatter: function (params) {
        var p = params[0];
        return p.name + '<br/>覆盖度：' + p.value + '%';
      }
    },
    xAxis: {
      type: 'value',
      max: 100,
      axisLabel: { color: muted, formatter: '{value}%' },
      splitLine: { lineStyle: { color: rule } }
    },
    yAxis: {
      type: 'category',
      data: dims.map(function (d) { return d.name; }),
      axisLabel: { color: ink, fontSize: 13 },
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false }
    },
    series: [{
      type: 'bar',
      data: dims.map(function (d) {
        return { value: d.score, itemStyle: { color: d.color, borderRadius: [0, 4, 4, 0] } };
      }),
      barWidth: 18,
      label: {
        show: true,
        position: 'right',
        formatter: '{c}%',
        color: muted,
        fontSize: 12
      }
    }]
  });

  window.addEventListener('resize', function () { chart.resize(); });
})();
