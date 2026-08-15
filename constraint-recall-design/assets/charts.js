(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue("--accent").trim();
  var accent2 = style.getPropertyValue("--accent2").trim();
  var ink = style.getPropertyValue("--ink").trim();
  var muted = style.getPropertyValue("--muted").trim();
  var rule = style.getPropertyValue("--rule").trim();
  var bg2 = style.getPropertyValue("--bg2").trim();
  var bad = style.getPropertyValue("--bad").trim();

  // --- Chart: budget comparison (article fixed 40% vs severity-aware) ---
  var el = document.getElementById("chart-budget");
  if (el && window.echarts) {
    var chart = echarts.init(el, null, { renderer: "svg" });
    chart.setOption({
      animation: false,
      color: [muted, accent],
      tooltip: {
        appendToBody: true,
        trigger: "item",
        formatter: function (p) {
          return p.name + "<br/>" + p.seriesName + "：" + p.value + "% 预算";
        }
      },
      legend: {
        bottom: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: muted, fontSize: 12 }
      },
      grid: { left: 8, right: 16, top: 40, bottom: 56, containLabel: true },
      xAxis: {
        type: "value",
        max: 70,
        axisLabel: { color: muted, formatter: "{value}%" },
        splitLine: { lineStyle: { color: rule } }
      },
      yAxis: {
        type: "category",
        data: [
          "正常量（5 条约束）",
          "warn 超量（40 条 warn）",
          "EMERGENCY 压缩级",
          "block 超量（60 条 block）"
        ],
        axisLabel: { color: ink, fontSize: 12 },
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false }
      },
      series: [
        {
          name: "文章方案（固定 40% 上限）",
          type: "bar",
          barWidth: 14,
          itemStyle: { color: muted, opacity: 0.55, borderRadius: [0, 3, 3, 0] },
          label: { show: true, position: "right", color: muted, fontSize: 11, formatter: "{c}%" },
          data: [10, 40, 8, 40]
        },
        {
          name: "本方案（严重度感知）",
          type: "bar",
          barWidth: 14,
          itemStyle: {
            borderRadius: [0, 3, 3, 0],
            color: function (p) {
              return p.dataIndex === 3 ? bad : accent;
            }
          },
          label: { show: true, position: "right", color: ink, fontSize: 11, fontWeight: 600, formatter: "{c}%" },
          data: [10, 37, 15, 65]
        }
      ]
    });
    window.addEventListener("resize", function () {
      chart.resize();
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
