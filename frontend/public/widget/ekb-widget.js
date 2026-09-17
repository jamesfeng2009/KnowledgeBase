/* EKB 网站嵌入 Widget — 加载即渲染提问浮窗。
 *
 * 使用方式（第三方网站）：
 *   <script>
 *     window.EKBWidget = { baseUrl: "https://kb.example.com", token: "换取的widget令牌" };
 *   </script>
 *   <script src="https://kb.example.com/widget/ekb-widget.js" defer></script>
 */
(function () {
  "use strict";

  var DEFAULT_BASE = "";

  function config() {
    return window.EKBWidget || {};
  }

  function buildCard() {
    var host = document.createElement("div");
    host.id = "ekb-widget-host";
    host.style.cssText =
      "position:fixed;right:20px;bottom:20px;z-index:99999;width:340px;" +
      "font-family:-apple-system,'PingFang SC',sans-serif;box-shadow:0 4px 24px rgba(0,0,0,.15);" +
      "border-radius:12px;overflow:hidden;background:#fff;display:none;";

    var header = document.createElement("div");
    header.style.cssText = "background:#2563eb;color:#fff;padding:10px 14px;font-size:14px;font-weight:600;";
    header.textContent = "企业知识库助手";

    var body = document.createElement("div");
    body.style.cssText = "padding:12px;";

    var input = document.createElement("input");
    input.placeholder = "输入问题…";
    input.style.cssText = "width:100%;box-sizing:border-box;padding:8px;border:1px solid #ddd;border-radius:6px;margin-bottom:8px;";

    var button = document.createElement("button");
    button.textContent = "提问";
    button.style.cssText = "width:100%;padding:8px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;";

    var result = document.createElement("div");
    result.style.cssText = "margin-top:10px;font-size:13px;color:#333;max-height:280px;overflow:auto;";

    body.appendChild(input);
    body.appendChild(button);
    body.appendChild(result);
    host.appendChild(header);
    host.appendChild(body);
    document.body.appendChild(host);

    function ask() {
      var cfg = config();
      var question = input.value.trim();
      if (!question) return;
      result.innerHTML = "思考中…";
      fetch((cfg.baseUrl || DEFAULT_BASE) + "/api/v1/widget/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: cfg.token, question: question }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.code !== 0) {
            result.innerHTML = "<span style='color:#dc2626'>" + (data.message || "请求失败") + "</span>";
            return;
          }
          var items = data.data.results || [];
          if (!items.length) {
            result.innerHTML = "未找到相关内容";
            return;
          }
          result.innerHTML = items
            .map(function (it) {
              return "<div style='margin-bottom:8px'><b>" + escapeHtml(it.title) + "</b>" +
                "<p style='margin:4px 0;color:#666'>" + escapeHtml(it.snippet) + "</p></div>";
            })
            .join("");
        })
        .catch(function () {
          result.innerHTML = "<span style='color:#dc2626'>网络错误</span>";
        });
    }

    button.addEventListener("click", ask);
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") ask(); });

    function escapeHtml(s) {
      var d = document.createElement("div");
      d.textContent = s || "";
      return d.innerHTML;
    }

    // 悬浮球切换
    var fab = document.createElement("div");
    fab.textContent = "?";
    fab.style.cssText =
      "position:fixed;right:20px;bottom:20px;z-index:99998;width:48px;height:48px;" +
      "border-radius:50%;background:#2563eb;color:#fff;text-align:center;line-height:48px;" +
      "font-size:22px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.25);";
    fab.addEventListener("click", function () {
      host.style.display = host.style.display === "none" ? "block" : "none";
    });
    document.body.appendChild(fab);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildCard);
  } else {
    buildCard();
  }
})();
