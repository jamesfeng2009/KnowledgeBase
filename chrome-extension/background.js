// EKB 剪藏助手 — Service Worker
// 职责：右键菜单创建、剪藏请求发送（上传到知识库 /api/v1/documents/clip）。

const DEFAULT_BASE_URL = "http://localhost:8000";

// 创建右键菜单
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "ekb-clip-selection",
    title: "剪藏到企业知识库",
    contexts: ["selection"],
  });
});

// 菜单点击 → 发送剪藏请求
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "ekb-clip-selection") return;
  const selection = info.selectionText || "";
  if (!selection.trim()) return;
  clipText(selection, tab?.title || "");
});

async function clipText(text, title) {
  const { baseUrl, apiKey, kbId } = await chrome.storage.sync.get({
    baseUrl: DEFAULT_BASE_URL,
    apiKey: "",
    kbId: "",
  });
  if (!apiKey || !kbId) {
    notifyError("请在插件弹窗中配置 API Key 与知识库 ID");
    return;
  }
  try {
    const resp = await fetch(`${baseUrl}/api/v1/documents/clip`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${apiKey}`,
        "X-API-Key": apiKey,
      },
      body: JSON.stringify({
        kb_id: kbId,
        title: title || "网页剪藏",
        content: text,
        source_url: (await getActiveTabUrl()) || "",
      }),
    });
    const data = await resp.json();
    if (!resp.ok || (data.code && data.code !== 0)) {
      throw new Error(data.message || `HTTP ${resp.status}`);
    }
    chrome.notifications?.create({
      type: "basic",
      iconUrl: "icons/icon48.png",
      title: "剪藏成功",
      message: `已剪藏 ${text.length} 字到知识库`,
    });
  } catch (err) {
    notifyError(`剪藏失败：${err.message}`);
  }
}

function notifyError(message) {
  chrome.notifications?.create({
    type: "basic",
    iconUrl: "icons/icon48.png",
    title: "EKB 剪藏",
    message,
  });
  console.error("[EKB] ", message);
}

async function getActiveTabUrl() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab?.url || "";
  } catch {
    return "";
  }
}
