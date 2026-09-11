"""内置合成 smoke 语料 — 离线可跑、CI 可回归，无需真实客户 PDF。

语料覆盖四个评测维度：
    fin_report.pdf      财务数字文本（scan=False）→ 文本抽取 P·R
    contract_scan.pdf   合同条款（scan=True，文本层即 OCR 真值）→ CER
    statements.html     财务报表表格（双层表头 + 合并单元格）→ 表格还原 P·R
    whitepaper.html     技术白皮书（公式 LaTeX + 表格 + 正文）→ 公式还原

生成结果写入语料目录（默认 eval_datasets/parse_corpus/），并产出与真实语料同
格式的 manifest.jsonl，保证"合成可跑、真实可替换"。
"""

from __future__ import annotations

import os
from typing import List

from .schema import CellSpan, ParseTruth, TableTruth, write_manifest


# ======================================================================
# 极简 PDF writer（无第三方依赖）— 只支持单页 ASCII 文本行
# ======================================================================

def _esc_pdf(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _pdf_objects(body: bytes) -> bytes:
    """把文本页拼成最小合法 PDF（带正确 xref 偏移）。"""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%b\nendstream" % (len(body) + 1, body),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(b"%d 0 obj\n" % i)
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref_pos = len(out)
    out.extend(b"xref\n0 %d\n" % (len(objects) + 1))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets:
        out.extend(b"%010d 00000 n \n" % off)
    out.extend(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_pos)
    )
    return bytes(out)


def _text_page_pdf(lines: List[str]) -> bytes:
    """把若干文本行渲染为单页 PDF。"""
    stream = bytearray(b"BT\n/F1 12 Tf\n72 720 Td\n")
    for ln in lines:
        stream.extend(b"(%s) Tj\n" % _esc_pdf(ln).encode("ascii", "replace"))
        stream.extend(b"0 -14 Td\n")
    stream.extend(b"ET")
    return _pdf_objects(bytes(stream))


# ======================================================================
# 合成语料内容
# ======================================================================

_FIN_LINES = [
    "2024 Annual Financial Statements",
    "Revenue  CNY 12,840,000  Cost  CNY 7,620,000  Profit  CNY 5,220,000",
    "Operating income CNY 1,410,000  Operating expense CNY 620,000",
    "Net cash flow from operations CNY 3,890,000",
    "Total assets CNY 28,450,000  Total liabilities CNY 9,730,000",
    "Equity attributable to shareholders CNY 18,720,000",
    "Diluted earnings per share CNY 2.31",
]

_CONTRACT_LINES = [
    "Sales Agency Agreement",
    "Party A: Beijing Ruihe Technology Co., Ltd.",
    "Party B: Shanghai Yaodu Trading Co., Ltd.",
    "1. Agency scope: exclusive distribution of Ruihe smart terminals in East China.",
    "2. Commission: 8% of net invoice amount, payable within 30 days of month end.",
    "3. Minimum annual purchase commitment: CNY 3,200,000.",
    "4. Breach of contract: defaulting party pays liquidated damages of CNY 200,000.",
    "Signed on the 15th day of March, 2025, effective for two years.",
]

_FIN_TABLE_ROWS = 4
_FIN_TABLE_COLS = 4
# 物理行模型：双层表头(row0-1) + 两行数据。Item 单元格 rowspan=2 跨表头两行，
# Amount 单元格 colspan=3 覆盖收入/成本/利润三列（合并区后续单元格留空）。
_FIN_TABLE_CELLS = [
    ["Item", "Amount", "", ""],
    ["", "Revenue", "Cost", "Profit"],
    ["First quarter", "2,910", "1,680", "1,230"],
    ["Second quarter", "3,120", "1,890", "1,230"],
]
_FIN_TABLE_SPANS = [
    CellSpan(row=0, col=0, row_span=2, col_span=1),   # Item 跨两行
    CellSpan(row=0, col=1, row_span=1, col_span=3),   # Amount 跨三列
]

_WHITE_TABLE = TableTruth(
    rows=3,
    cols=4,
    cells=[
        ["Region", "Q1 Shipment", "Q2 Shipment", "Total"],
        ["East China", "1,200", "1,350", "2,550"],
        ["South China", "980", "1,100", "2,080"],
    ],
    spans=[CellSpan(row=0, col=0, row_span=1, col_span=1)],
    header_rows=1,
)


def _html(name: str, body: str) -> bytes:
    return (
        "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        f"<title>{name}</title></head><body>{body}</body></html>"
    ).encode("utf-8")


# ======================================================================
# 语料生成
# ======================================================================

def generate(corpus_dir: str | None = None, seed: int = 2026) -> list[ParseTruth]:
    """生成合成语料并写 manifest，返回真值列表。"""
    if corpus_dir is None:
        corpus_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "eval_datasets", "parse_corpus"
        )
    corpus_dir = os.path.abspath(corpus_dir)
    os.makedirs(corpus_dir, exist_ok=True)

    truths: list[ParseTruth] = []

    # 1. 财务 PDF（文本层，scan=False）→ 文本抽取
    pdf1 = "fin_report.pdf"
    with open(os.path.join(corpus_dir, pdf1), "wb") as fh:
        fh.write(_text_page_pdf(_FIN_LINES))
    truths.append(
        ParseTruth(
            doc_id="fin_report",
            source=pdf1,
            kind="pdf",
            scan=False,
            text="\n".join(_FIN_LINES),
        )
    )

    # 2. 合同扫描 PDF（scan=True，文本层作为 OCR 真值）→ CER
    pdf2 = "contract_scan.pdf"
    with open(os.path.join(corpus_dir, pdf2), "wb") as fh:
        fh.write(_text_page_pdf(_CONTRACT_LINES))
    truths.append(
        ParseTruth(
            doc_id="contract_scan",
            source=pdf2,
            kind="pdf",
            scan=True,
            text="\n".join(_CONTRACT_LINES),
        )
    )

    # 3. 财务报表 HTML（双层表头 + 合并单元格）→ 表格还原
    html1 = "statements.html"
    stmt_body = (
        "<h1>2024 Quarterly Statements</h1><p>Consolidated revenue grew 7.2% "
        "year on year, with cost control improving net margin.</p>"
        "<table border=\"1\">"
        "<thead><tr><th rowspan=\"2\">Item</th>"
        "<th colspan=\"3\">Amount (CNY '000)</th></tr>"
        "<tr><th>Revenue</th><th>Cost</th><th>Profit</th></tr></thead>"
        "<tbody>"
        "<tr><td>First quarter</td><td>2,910</td><td>1,680</td><td>1,230</td></tr>"
        "<tr><td>Second quarter</td><td>3,120</td><td>1,890</td><td>1,230</td></tr>"
        "</tbody></table>"
        f"<p>{_FIN_LINES[1]}</p>"
    )
    with open(os.path.join(corpus_dir, html1), "wb") as fh:
        fh.write(_html("statements", stmt_body))
    truths.append(
        ParseTruth(
            doc_id="statements",
            source=html1,
            kind="html",
            scan=False,
            text="Consolidated revenue grew 7.2% year on year, with cost control "
            "improving net margin.\n" + "\n".join(_FIN_LINES[1:]),
            tables=[
                TableTruth(
                    rows=_FIN_TABLE_ROWS,
                    cols=_FIN_TABLE_COLS,
                    cells=_FIN_TABLE_CELLS,
                    spans=_FIN_TABLE_SPANS,
                    header_rows=2,
                )
            ],
        )
    )

    # 4. 技术白皮书 HTML（公式 LaTeX + 表格）→ 公式还原
    html2 = "whitepaper.html"
    wp_body = (
        "<h1>Latency Model for Distributed Caching</h1>"
        "<p>We model end-to-end latency as a function of hit ratio. The core "
        "formula is presented below:</p>"
        "<p class=\"formula\">T = h * T_cache + (1 - h) * T_miss</p>"
        "<p>Where T_cache is the cache access cost and T_miss the miss "
        "penalty. When hit ratio approaches unity, latency converges to the "
        "cache floor.</p>"
        f"<table border=\"1\">{_WHITE_TABLE_TO_HTML()}</table>"
    )
    with open(os.path.join(corpus_dir, html2), "wb") as fh:
        fh.write(_html("whitepaper", wp_body))
    truths.append(
        ParseTruth(
            doc_id="whitepaper",
            source=html2,
            kind="html",
            scan=False,
            text="We model end-to-end latency as a function of hit ratio. "
            "T = h * T_cache + (1 - h) * T_miss. Where T_cache is the cache "
            "access cost and T_miss the miss penalty.",
            tables=[_WHITE_TABLE],
            formulas=["T = h \\cdot T_{cache} + (1 - h) \\cdot T_{miss}"],
        )
    )

    write_manifest(os.path.join(corpus_dir, "manifest.jsonl"), truths)
    return truths


def _WHITE_TABLE_TO_HTML() -> str:
    return (
        "<thead><tr><th>Region</th><th>Q1 Shipment</th><th>Q2 Shipment</th>"
        "<th>Total</th></tr></thead><tbody>"
        "<tr><td>East China</td><td>1,200</td><td>1,350</td><td>2,550</td></tr>"
        "<tr><td>South China</td><td>980</td><td>1,100</td><td>2,080</td></tr>"
        "</tbody>"
    )


def main() -> None:
    corpus_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "eval_datasets", "parse_corpus"
    )
    truths = generate(corpus_dir)
    print(f"生成 {len(truths)} 份合成语料于 {os.path.abspath(corpus_dir)}/")
    for t in truths:
        print(f"  - {t.source}  kind={t.kind}  scan={t.scan}  "
              f"tables={len(t.tables)}  formulas={len(t.formulas)}")


if __name__ == "__main__":
    main()