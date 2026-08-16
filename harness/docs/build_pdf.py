"""
Regenerate the submission PDFs (scoping_note.pdf, optimization_writeup.pdf)
from their source Markdown after an edit. Not part of the harness itself —
a one-off doc-build utility, kept out of requirements.txt on purpose.

Usage (from repo root):
    pip install markdown
    python harness/docs/build_pdf.py harness/docs/scoping_note.md "Title" /tmp/out.html
    # then print /tmp/out.html to PDF with headless Chrome/Edge, e.g.:
    chrome --headless --disable-gpu --no-pdf-header-footer \
        --print-to-pdf=harness/docs/scoping_note.pdf /tmp/out.html
"""
import sys
import markdown
from pathlib import Path

PRINT_CSS = """
<style>
  @page { size: Letter; margin: 0.45in 0.7in; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 9.1pt;
    line-height: 1.26;
    color: #1a1d24;
    max-width: 100%;
  }
  h1 {
    font-size: 14pt; margin: 0 0 2pt; padding-bottom: 4pt;
    border-bottom: 2px solid #1a1d24;
  }
  h2 {
    font-size: 10.6pt; margin: 6.5pt 0 2.5pt; padding-bottom: 1.5pt;
    border-bottom: 1px solid #c8ccd6; color: #0b3d91;
  }
  h3 { font-size: 10pt; margin: 7pt 0 2pt; }
  p { margin: 2.5pt 0; }
  ul, ol { margin: 2.5pt 0; padding-left: 15pt; }
  li { margin: 1pt 0; }
  strong { color: #0b3d91; }
  code {
    font-family: "SF Mono", Consolas, "Cascadia Mono", monospace;
    font-size: 8.3pt; background: #f0f2f7; padding: 1px 4px; border-radius: 3px;
  }
  pre {
    font-family: "SF Mono", Consolas, "Cascadia Mono", monospace;
    font-size: 7.8pt; background: #14171f; color: #e3e6ee;
    padding: 7pt 10pt; border-radius: 5px; overflow-x: auto;
    line-height: 1.32; white-space: pre-wrap; word-wrap: break-word;
  }
  pre code { background: none; padding: 0; color: inherit; }
  table {
    border-collapse: collapse; width: 100%; margin: 5pt 0 7pt;
    font-size: 8.3pt; page-break-inside: avoid;
  }
  th, td {
    border: 1px solid #c8ccd6; padding: 2.5pt 5pt; text-align: left;
    vertical-align: top;
  }
  th { background: #eef1f8; font-weight: 600; color: #0b3d91; }
  tr:nth-child(even) td { background: #f8f9fc; }
  blockquote {
    margin: 5pt 0; padding: 4pt 9pt; border-left: 3px solid #0b3d91;
    background: #f0f4fb; color: #333; font-size: 9pt;
  }
  hr { border: none; border-top: 1px solid #c8ccd6; margin: 8pt 0; }
  .doc-meta {
    font-size: 8.5pt; color: #767a86; margin-top: -2pt; margin-bottom: 10pt;
  }
</style>
"""

def build(md_path: str, title: str, out_path: str):
    text = Path(md_path).read_text(encoding="utf-8")
    html_body = markdown.markdown(text, extensions=["tables", "fenced_code", "nl2br"])
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>{PRINT_CSS}</head>
<body>{html_body}</body></html>"""
    Path(out_path).write_text(html, encoding="utf-8")
    print(f"wrote {out_path}")

if __name__ == "__main__":
    md_path, title, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    build(md_path, title, out_path)
