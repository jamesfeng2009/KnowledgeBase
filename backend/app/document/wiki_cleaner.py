"""Wiki HTML 清洗器 — 将 Confluence/飞书/通用 Wiki HTML 清洗为语义化 HTML。

单一职责：只负责"把 Wiki HTML 变成 chunker 可消费的语义化 HTML"，
完全不知道文档来自哪个平台。适配器层负责从外部平台拉取原始 HTML。

五层清洗管线：
    Step 1: 命名空间宏剥离（Confluence ac:/ri: 标签）
    Step 2: 装饰性标签剥离（script/style/装饰 div/span/飞书 emoji）
    Step 3: 属性清洗（非白名单标签 unwrap + 非白名单属性清除）
    Step 4: 结构规范化（img → [图片: ...]，飞书 heading → h1-h6）
    Step 5: 空标签清理 + 空白压缩

依赖：beautifulsoup4 + lxml（项目当前未安装，需加到 requirements.txt）
降级：bs4 未安装时回退到正则清洗（保留 h1-h6 标签，比原 _parse_html 更保守）
"""
from __future__ import annotations

import re

from app.utils.logger import get_logger

log = get_logger(__name__)

# ======================================================================
# 常量定义
# ======================================================================

# Confluence 命名空间标签 — unwrap（子节点上提到父级，保留语义内容）
# ac:structured-macro 需在此处：代码宏(info/tip/code)的正文已被 _UNWRAP_NS 提升到 macro 内部，
# macro 自身再 unwrap 将内容提升到父级。顺序：先 unwrap body → 再 unwrap macro。
_UNWRAP_NS: frozenset[str] = frozenset(
    {
        "ac:structured-macro",  # 宏外壳（代码/提示/警告等，正文已从 body unwrap 提升）
        "ac:rich-text-body",  # 宏内富文本正文
        "ac:plain-text-body",  # 代码宏纯文本
        "ac:link-body",  # 链接文本
        "ac:task-body",  # 任务列表正文
    }
)

# Confluence 命名空间标签 — drop（整个移除，子内容已 unwrap 上提）
# 注意：ac:structured-macro 不在此处 — 它需要 unwrap 而非 decompose，
# 因为 ac:plain-text-body / ac:rich-text-body 的内容已被 unwrap 提升到 macro 内部，
# 此时 macro 需要再次 unwrap 将内容提升到父级，否则 decompose 会丢弃已提升的内容。
_DROP_NS: frozenset[str] = frozenset(
    {
        "ac:parameter",  # 宏参数（纯配置，无正文）
        "ac:default-parameter",  # 默认参数
        "ac:layout",  # 布局容器（纯结构，内容已 unwrap）
        "ac:layout-section",  # 布局段（纯结构，内容已 unwrap）
        "ac:placeholder",  # 占位符
        "ri:attachment",  # 附件引用
        "ri:resource",  # 资源引用
        "ri:page",  # 页面引用（仅引用关系，正文已在别处）
    }
)

# 特殊命名空间标签 — 需提取属性为文本/链接
_SPECIAL_NS: frozenset[str] = frozenset({"ri:user", "ri:url"})

# 语义标签白名单 — 不在白名单内的标签全部 unwrap（保留子节点文本）
_TAG_WHITELIST: frozenset[str] = frozenset(
    {
        # 标题（chunker 按 h1-h6 分块）
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        # 段落/换行
        "p",
        "br",
        "hr",
        # 表格（接口参数表/字段映射表）
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "colgroup",
        "col",
        # 列表
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        # 代码/引用
        "pre",
        "code",
        "blockquote",
        # 媒体/链接
        "img",
        "a",
        # 文本格式
        "strong",
        "em",
        "b",
        "i",
        "u",
    }
)

# 属性白名单 — 仅这些标签保留这些属性，其余属性清除
_ATTR_WHITELIST: dict[str, frozenset[str]] = {
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
}


# ======================================================================
# 公共入口
# ======================================================================


def clean_wiki_html(html: str, source: str = "auto") -> str:
    """清洗 Wiki HTML，输出语义化 HTML 供 chunker 消费。

    Args:
        html: 原始 Wiki HTML（Confluence/飞书导出）。
        source: 来源标识 ``"confluence"`` / ``"feishu"`` / ``"generic"`` / ``"auto"``。
            ``auto`` 模式通过特征检测自动判断来源。

    Returns:
        清洗后的语义化 HTML 字符串，保留 h1-h6/table/ul-ol/pre/img。
    """
    if not html or not html.strip():
        return ""

    # 自动检测来源
    if source == "auto":
        source = _detect_source(html)

    try:
        from bs4 import BeautifulSoup, NavigableString
    except ImportError:
        log.warning("wiki_cleaner.bs4_not_installed")
        return _fallback_clean(html, source)

    soup = BeautifulSoup(html, "lxml")

    # Step 1: 命名空间宏剥离
    _strip_namespaces(soup, NavigableString)

    # Step 2: 装饰性标签剥离
    _strip_decorations(soup, source)

    # Step 3: 属性清洗 + 非白名单标签 unwrap
    _clean_tags_and_attrs(soup)

    # Step 4: 结构规范化
    _normalize_structure(soup, NavigableString)

    # Step 5: 清除空标签 + 压缩空白
    _remove_empty_tags(soup)

    result = str(soup)
    # 压缩连续空行
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def _detect_source(html: str) -> str:
    """自动检测 Wiki HTML 来源。"""
    if "ac:structured-macro" in html or "ri:attachment" in html:
        return "confluence"
    if "data-record-type" in html or "data-record-id" in html:
        return "feishu"
    return "generic"


# ======================================================================
# Step 1: 命名空间宏剥离
# ======================================================================


def _strip_namespaces(soup, NavigableString) -> None:
    """剥离 Confluence 命名空间宏（ac:/ri:）。

    处理顺序至关重要：先 unwrap 内容容器（子节点上提），再 drop 宏外壳。
    如果顺序反了会丢失正文。
    """
    from bs4 import Tag

    # 1. 先 unwrap 内容容器（子节点上提到父级）
    for tag in soup.find_all(lambda t: isinstance(t, Tag) and t.name in _UNWRAP_NS):
        tag.unwrap()

    # 2. 特殊处理 ri:user / ri:url
    for tag in soup.find_all(lambda t: isinstance(t, Tag) and t.name in _SPECIAL_NS):
        if tag.name == "ri:user":
            username = tag.get("ri:username", "") or tag.get("ri:account-id", "")
            tag.replace_with(NavigableString(f"@{username}" if username else ""))
        elif tag.name == "ri:url":
            url = tag.get("ri:value", "")
            if url:
                new_tag = soup.new_tag("a", href=url)
                new_tag.string = url
                tag.replace_with(new_tag)
            else:
                tag.decompose()

    # 3. 再 drop 残留的宏外壳和参数
    for tag in soup.find_all(lambda t: isinstance(t, Tag) and t.name in _DROP_NS):
        tag.decompose()


# ======================================================================
# Step 2: 装饰性标签剥离
# ======================================================================


def _strip_decorations(soup, source: str) -> None:
    """剥离装饰性标签 — 通用 + 平台特有。"""
    # 通用：script/style/noscript/iframe 完全移除
    for tag in soup.find_all(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    # 内联样式 span/div → unwrap（去标签保文本）
    for tag in soup.find_all(["span", "div"], attrs={"style": True}):
        tag.unwrap()

    if source == "confluence":
        # Confluence layout div
        for tag in soup.select("div[class^='cell-'], div[class^='columnLayout']"):
            tag.unwrap()
        # Confluence metadata 宏
        for tag in soup.select("div.conf-macro[data-macro-name='metadata']"):
            tag.decompose()

    if source == "feishu":
        # 飞书 emoji span → drop
        for tag in soup.select("span[data-type='emoji']"):
            tag.decompose()
        # 飞书 data-record div → 转 heading 或 unwrap
        for tag in soup.find_all(attrs={"data-record-type": True}):
            rtype = tag.get("data-record-type", "")
            tag.attrs.clear()
            if rtype.startswith("heading") and len(rtype) >= 8:
                level = rtype[-1]
                if level.isdigit() and 1 <= int(level) <= 6:
                    tag.name = f"h{level}"
                else:
                    tag.unwrap()
            else:
                tag.unwrap()


# ======================================================================
# Step 3: 属性清洗 + 非白名单标签 unwrap
# ======================================================================


def _clean_tags_and_attrs(soup) -> None:
    """清除非白名单属性，unwrap 非白名单标签。"""
    from bs4 import Tag

    for tag in soup.find_all(True):  # 所有 Tag
        if not isinstance(tag, Tag):
            continue
        # 非白名单标签 → unwrap（保留子节点文本）
        if tag.name not in _TAG_WHITELIST:
            tag.unwrap()
            continue
        # 白名单标签 → 清除非白名单属性
        allowed = _ATTR_WHITELIST.get(tag.name, frozenset())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag[attr]


# ======================================================================
# Step 4: 结构规范化
# ======================================================================


def _normalize_structure(soup, NavigableString) -> None:
    """结构规范化 — img → [图片: ...] 内联标注。"""
    # img → [图片: alt或src]（与 base.py ParsedSection.image_desc 格式一致）
    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt", "")
        label = f"[图片: {alt or src}]"
        img.replace_with(NavigableString(label))


# ======================================================================
# Step 5: 空标签清理
# ======================================================================


def _remove_empty_tags(soup) -> None:
    """清除空标签（unwrap 后产生的空壳）。"""
    from bs4 import Tag

    # 多轮清理（unwrap 可能产生新的空标签）
    changed = True
    while changed:
        changed = False
        for tag in soup.find_all(True):
            if not isinstance(tag, Tag):
                continue
            # 不清理表格单元格和自闭合标签
            if tag.name in ("td", "th", "br", "hr", "img", "col"):
                continue
            # 有文本内容或包含图片 → 保留
            if tag.get_text(strip=True):
                continue
            # 空标签 → 移除
            tag.decompose()
            changed = True


# ======================================================================
# 降级清洗（bs4 未安装时）
# ======================================================================


def _fallback_clean(html: str, source: str) -> str:
    """bs4 未安装时的降级清洗 — 比原 _parse_html 更保守，保留标题标签。

    原有 _parse_html 会去掉所有标签（包括 h1-h6），导致 chunker 无法结构化分块。
    本降级至少保留 h1-h6 和 table 标签，让 chunker 的 _split_html 能工作。
    """
    # 去除 script/style/noscript/iframe
    clean = re.sub(
        r"<(script|style|noscript|iframe)[^>]*>.*?</\1>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    if source == "confluence":
        # 去命名空间标签（ac:/ri:）— unwrap 效果：去标签保内容
        clean = re.sub(r"</?(ac|ri):[^>]*>", "", clean)

    if source == "feishu":
        # 去飞书 emoji span
        clean = re.sub(
            r'<span[^>]*data-type=["\']emoji["\'][^>]*>.*?</span>',
            "",
            clean,
            flags=re.DOTALL,
        )
        # 飞书 data-record div → unwrap（去标签保内容）
        clean = re.sub(
            r"</?div[^>]*data-record-type=[^>]*>",
            "",
            clean,
        )

    # 去内联样式 span/div（保文本）
    clean = re.sub(r"</?(span|div)[^>]*>", "", clean)

    # 压缩空白
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean
