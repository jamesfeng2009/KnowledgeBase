// EKB 剪藏助手 — Popup 逻辑：读取/保存配置。

const DEFAULTS = {
  baseUrl: "http://localhost:8000",
  apiKey: "",
  kbId: "",
};

document.addEventListener("DOMContentLoaded", async () => {
  const saved = await chrome.storage.sync.get(DEFAULTS);
  document.getElementById("baseUrl").value = saved.baseUrl;
  document.getElementById("apiKey").value = saved.apiKey;
  document.getElementById("kbId").value = saved.kbId;
});

document.getElementById("save").addEventListener("click", async () => {
  const config = {
    baseUrl: document.getElementById("baseUrl").value.trim() || DEFAULTS.baseUrl,
    apiKey: document.getElementById("apiKey").value.trim(),
    kbId: document.getElementById("kbId").value.trim(),
  };
  await chrome.storage.sync.set(config);
  const status = document.getElementById("status");
  status.textContent = "已保存";
  status.className = "";
  setTimeout(() => (status.textContent = ""), 1500);
});
