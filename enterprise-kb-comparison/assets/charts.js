(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var accent3 = style.getPropertyValue('--accent3').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var bg3 = style.getPropertyValue('--bg3').trim();

  // --- Chart 1: Radar ---
  var radarChart = echarts.init(document.getElementById('chart-radar'), null, { renderer: 'svg' });
  radarChart.setOption({
    animation: false,
    tooltip: {
      trigger: 'item',
      appendToBody: true,
      backgroundColor: bg3,
      borderColor: rule,
      textStyle: { color: ink }
    },
    legend: {
      data: ['EKB 自研项目', '神光知识库', '知微行易（笃行）'],
      bottom: 10,
      textStyle: { color: muted, fontSize: 13 },
      itemGap: 20
    },
    radar: {
      indicator: [
        { name: '对话智能', max: 10 },
        { name: '上下文工程', max: 10 },
        { name: 'RAG引擎', max: 10 },
        { name: '前端UI', max: 10 },
        { name: '多模态', max: 10 },
        { name: '权限安全', max: 10 },
        { name: '架构扩展性', max: 10 },
        { name: '可观测性', max: 10 },
        { name: '测试覆盖', max: 10 },
        { name: '系统集成', max: 10 }
      ],
      shape: 'polygon',
      radius: '68%',
      center: ['50%', '48%'],
      axisName: {
        color: muted,
        fontSize: 12
      },
      splitArea: {
        areaStyle: {
          color: [bg2, 'transparent', bg2, 'transparent', bg2]
        }
      },
      splitLine: {
        lineStyle: { color: rule }
      },
      axisLine: {
        lineStyle: { color: rule }
      }
    },
    series: [{
      type: 'radar',
      data: [
        {
          value: [10, 10, 9, 3, 7, 9, 9, 8, 10, 8],
          name: 'EKB 自研项目',
          lineStyle: { color: accent, width: 2 },
          areaStyle: { color: accent, opacity: 0.15 },
          itemStyle: { color: accent }
        },
        {
          value: [4, 5, 9, 9, 9, 7, 6, 9, 6, 3],
          name: '神光知识库',
          lineStyle: { color: accent2, width: 2 },
          areaStyle: { color: accent2, opacity: 0.15 },
          itemStyle: { color: accent2 }
        },
        {
          value: [5, 5, 7, 6, 5, 7, 8, 6, 5, 10],
          name: '知微行易（笃行）',
          lineStyle: { color: accent3, width: 2, type: 'dashed' },
          areaStyle: { color: accent3, opacity: 0.1 },
          itemStyle: { color: accent3 }
        }
      ]
    }]
  });
  window.addEventListener('resize', function() { radarChart.resize(); });

  // --- Chart 2: Score Bar Chart ---
  var scoreChart = echarts.init(document.getElementById('chart-score'), null, { renderer: 'svg' });
  scoreChart.setOption({
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      appendToBody: true,
      backgroundColor: bg3,
      borderColor: rule,
      textStyle: { color: ink }
    },
    legend: {
      data: ['EKB', '知微行易', '神光'],
      bottom: 5,
      textStyle: { color: muted, fontSize: 13 },
      itemGap: 25
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '15%',
      top: '5%',
      containLabel: true
    },
    xAxis: {
      type: 'value',
      max: 10,
      axisLabel: { color: muted, fontSize: 12 },
      axisLine: { lineStyle: { color: rule } },
      splitLine: { lineStyle: { color: rule } }
    },
    yAxis: {
      type: 'category',
      data: ['对话智能', '上下文工程', 'RAG引擎', '前端UI', '多模态', '权限安全', '架构扩展', '可观测性', '测试覆盖', '系统集成', '产品成熟度'],
      axisLabel: { color: muted, fontSize: 12 },
      axisLine: { lineStyle: { color: rule } }
    },
    series: [
      {
        name: 'EKB',
        type: 'bar',
        data: [10, 10, 9, 3, 7, 9, 9, 8, 10, 8, 5],
        itemStyle: { color: accent, borderRadius: [0, 3, 3, 0] },
        barGap: '10%'
      },
      {
        name: '知微行易',
        type: 'bar',
        data: [5, 5, 7, 6, 5, 7, 8, 6, 5, 10, 9],
        itemStyle: { color: accent3, borderRadius: [0, 3, 3, 0] }
      },
      {
        name: '神光',
        type: 'bar',
        data: [4, 5, 9, 9, 9, 7, 6, 9, 6, 3, 8],
        itemStyle: { color: accent2, borderRadius: [0, 3, 3, 0] }
      }
    ]
  });
  window.addEventListener('resize', function() { scoreChart.resize(); });
})();
