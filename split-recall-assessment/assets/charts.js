(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue("--accent").trim();
  var accent2 = style.getPropertyValue("--accent2").trim();
  var ink = style.getPropertyValue("--ink").trim();
  var muted = style.getPropertyValue("--muted").trim();
  var rule = style.getPropertyValue("--rule").trim();
  var bg2 = style.getPropertyValue("--bg2").trim();

  // --- Chart: radar (现状 vs 目标态) ---
  var radarEl = document.getElementById("chart-radar");
  if (radarEl && window.echarts) {
    var radar = echarts.init(radarEl, null, { renderer: "svg" });
    var dims = [
      "多路并行召回",
      "确定性权限过滤",
      "Query侧规则拦截",
      "红线知识必召回",
      "写入时分类打标",
      "优先级预算合并",
      "工具层校验审计",
      "质量守护兜底"
    ];
    radar.setOption({
      animation: false,
      color: [accent, accent2],
      tooltip: {
        appendToBody: true,
        trigger: "item"
      },
      legend: {
        bottom: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: muted, fontSize: 12 }
      },
      radar: {
        indicator: dims.map(function (d) {
          return { name: d, max: 5 };
        }),
        center: ["50%", "48%"],
        radius: "62%",
        shape: "polygon",
        axisName: {
          color: ink,
          fontSize: 12,
          lineHeight: 16
        },
        nameGap: 12,
        splitNumber: 5,
        splitLine: { lineStyle: { color: rule } },
        splitArea: { areaStyle: { color: [bg2, "rgba(14,95,165,0.03)"] } },
        axisLine: { lineStyle: { color: rule } }
      },
      series: [
        {
          type: "radar",
          symbolSize: 5,
          data: [
            {
              name: "当前项目现状",
              value: [5, 5, 4, 1, 1, 2, 4, 4],
              itemStyle: { color: accent },
              lineStyle: { color: accent, width: 2 },
              areaStyle: { color: accent, opacity: 0.18 }
            },
            {
              name: "借鉴后目标态",
              value: [5, 5, 4, 5, 4, 4, 4, 4],
              itemStyle: { color: accent2 },
              lineStyle: { color: accent2, width: 2, type: "dashed" },
              areaStyle: { color: accent2, opacity: 0.08 }
            }
          ]
        }
      ]
    });
    window.addEventListener("resize", function () {
      radar.resize();
    });
  }

  // --- Mermaid init ---
  if (window.mermaid) {
    mermaid.initialize({
      startOnLoad: false,
      theme: "neutral",
      securityLevel: "loose",
      themeVariables: {
        primaryColor: bg2,
        primaryTextColor: ink,
        primaryBorderColor: rule,
        lineColor: muted,
        fontSize: "13px"
      }
    });
    mermaid.run({ querySelector: "pre.mermaid" });
  }
})();
