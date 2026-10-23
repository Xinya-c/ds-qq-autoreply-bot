import os
import re
import subprocess
import markdown

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "docs", "QQ自动回复系统-项目总结.md")
HTML = os.path.join(BASE, "dist", "_doc.html")
PDF = os.path.join(BASE, "dist", "QQ自动回复系统-项目总结.pdf")

with open(SRC, encoding="utf-8") as f:
    md_text = f.read()

body = markdown.markdown(
    md_text,
    extensions=["tables", "fenced_code", "toc", "sane_lists"],
)

css = """
@page { size: A4; margin: 16mm 14mm; }
* { box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", sans-serif;
  font-size: 10.5pt; line-height: 1.7; color: #1f2328; margin: 0;
}
h1 { font-size: 20pt; border-bottom: 3px solid #2f6feb; padding-bottom: 8px; margin: 0 0 14px; }
h2 { font-size: 14pt; margin: 22px 0 10px; padding-left: 9px; border-left: 4px solid #2f6feb; color: #0b3d91; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 16px 0 8px; color: #244; page-break-after: avoid; }
p { margin: 7px 0; }
ul, ol { margin: 7px 0; padding-left: 22px; }
li { margin: 3px 0; }
code { background: #f2f4f7; padding: 1px 4px; border-radius: 3px; font-family: Consolas, monospace; font-size: 9.2pt; color: #b5316b; }
pre { background: #f6f8fa; border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px 12px; overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; padding: 0; color: #1f2328; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 9.4pt; page-break-inside: avoid; }
th { background: #eef3fb; font-weight: 600; text-align: left; }
th, td { border: 1px solid #d5dce6; padding: 6px 9px; vertical-align: top; }
tr:nth-child(even) td { background: #fafbfd; }
blockquote { margin: 10px 0; padding: 8px 12px; background: #f4f7fb; border-left: 3px solid #9fb6d9; color: #44506a; }
hr { border: none; border-top: 1px solid #e3e8ef; margin: 18px 0; }
a { color: #2f6feb; text-decoration: none; }
strong { color: #0b3d91; }
h1, h2, h3 { page-break-after: avoid; }
"""

html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>QQ 自动回复系统 · 项目总结</title><style>%s</style></head><body>%s</body></html>""" % (css, body)

os.makedirs(os.path.dirname(HTML), exist_ok=True)
with open(HTML, "w", encoding="utf-8") as f:
    f.write(html)
print("HTML 已生成:", HTML, "%.1f KB" % (os.path.getsize(HTML) / 1024))

edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
cmd = [edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
       "--user-data-dir=" + os.path.join(os.environ.get("TEMP", "."), "edge_pdf_profile"),
       "--print-to-pdf=" + PDF, "file:///" + HTML.replace("\\", "/")]
r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
print("Edge 退出码:", r.returncode)
if r.stderr.strip():
    print("stderr:", r.stderr.strip()[:300])
print("PDF 存在:", os.path.exists(PDF), "%.1f KB" % (os.path.getsize(PDF) / 1024) if os.path.exists(PDF) else "")
