"""The readout as a standalone HTML page (readout.html), for people who open it in a browser
or a desktop app. A small Markdown subset, standard library only: headings, paragraphs,
bold, inline code, bullet and numbered lists (one level of nesting), tables, rules."""

import html
import re

CSS = """
:root { --fg:#1f2328; --muted:#59636e; --line:#d1d9e0; --bg:#ffffff; --soft:#f6f8fa; --ok:#1a7f37; --warn:#9a6700; --bad:#d1242f; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6edf3; --muted:#9198a1; --line:#3d444d; --bg:#0d1117; --soft:#151b23; } }
body { font: 15px/1.55 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 26px; margin: 0 0 16px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }
h1.details { font-size: 20px; margin-top: 40px; color: var(--muted); }
h2 { font-size: 19px; margin: 28px 0 10px; } h3 { font-size: 16px; margin: 18px 0 6px; }
section.minute { background: var(--soft); border: 1px solid var(--line); border-radius: 10px; padding: 4px 20px 12px; }
section.minute h2 { margin-top: 14px; }
p { margin: 8px 0; } ul, ol { margin: 6px 0 10px; padding-left: 24px; } li { margin: 3px 0; }
table { border-collapse: collapse; margin: 10px 0; font-size: 14px; } td { vertical-align: top; } th, td { border: 1px solid var(--line); padding: 5px 10px; }
th { background: var(--soft); text-align: left; }
code { background: var(--soft); padding: 1px 4px; border-radius: 4px; font-size: 13px; }
hr { border: 0; border-top: 1px solid var(--line); margin: 28px 0 12px; }
.prov { color: var(--muted); font-size: 12px; }
.go-yes { color: var(--ok); } .go-dir { color: var(--warn); } .go-no { color: var(--bad); }
"""


def _inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def _go_class(text):
    if "Ready for triage:" not in text:
        return ""
    after = text.split("Ready for triage:", 1)[1].strip(" *")
    if after.startswith("No"):
        return " go-no"
    return " go-yes" if after.startswith("Yes.") else " go-dir"


def to_html(markdown, title):
    lines = markdown.splitlines()
    out, i, in_minute = [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.startswith("## Summary"):
            out.append('<section class="minute"><h2>Summary</h2>')
            in_minute = True
        elif line.startswith("# "):
            if in_minute:
                out.append("</section>")
                in_minute = False
            out.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_minute:
                out.append("</section>")
                in_minute = False
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.strip() == "---":
            out.append("<hr>")
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"-+", c) for c in cells):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append("<table><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body) + "</table>")
            continue
        elif re.match(r"^(\s*)([-*]|\d+\.) ", line):
            ordered = bool(re.match(r"^\d+\.", line.strip()))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines) and re.match(r"^(\s*)([-*]|\d+\.) ", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                text = re.sub(r"^\s*([-*]|\d+\.) ", "", lines[i])
                if indent and items:
                    items[-1][1].append(text)
                else:
                    items.append([text, []])
                i += 1
            out.append(f"<{tag}>" + "".join(
                f"<li>{_inline(t)}" + ("<ul>" + "".join(f"<li>{_inline(s)}</li>" for s in subs) + "</ul>" if subs else "") + "</li>"
                for t, subs in items) + f"</{tag}>")
            continue
        elif line.startswith(("Provenance:", "Sources:", "Support:")):
            out.append(f'<p class="prov">{_inline(line)}</p>')
        elif line.strip():
            out.append(f'<p class="{_go_class(line).strip()}">{_inline(line)}</p>' if _go_class(line) else f"<p>{_inline(line)}</p>")
        i += 1
    if in_minute:
        out.append("</section>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f"<body>{''.join(out)}</body></html>")
