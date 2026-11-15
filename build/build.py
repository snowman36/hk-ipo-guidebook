#!/usr/bin/env python3
"""Guidebook 内容/版式分离构建器.

用法:
  python3 build/build.py extract   # 从 site/index.html 拆出 content/*.json + build/shell.html
  python3 build/build.py render    # 由 content/*.json + build/shell.html 生成 build/index.html
  python3 build/build.py compare   # 比对原始 index.html 与生成结果的可见文本

设计要点:
  - 能识别的组件 -> 结构化字段; 识别不了的整体降级为 {"type":"raw"}，保证零内容丢失。
  - 内联格式只用 **粗体** / `代码` / [文字](链接) 三种 markdown 方言，其余字符转义。
"""
from __future__ import annotations

import html as htmllib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "site" / "index.html"
BUILD = ROOT / "build"
SHELL = BUILD / "shell.html"
CONTENT = ROOT / "content"
OUT = BUILD / "index.html"

PAGE_IDS = ["cover", "sec0", "sec-intro", "sec1", "sec2", "sec3",
            "sec4", "sec5", "sec6", "sec7", "sec8"]

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}


# ---------------------------------------------------------------- mini DOM
class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children = []
        self.parent = parent

    @property
    def cls(self):
        return self.attrs.get("class", "")

    def has(self, name):
        return name in self.cls.split()

    def find_all(self, tag=None, cls=None):
        out = []
        for c in self.children:
            if isinstance(c, Node):
                if (tag is None or c.tag == tag) and (cls is None or c.has(cls)):
                    out.append(c)
                out.extend(c.find_all(tag, cls))
        return out

    def find(self, tag=None, cls=None):
        r = self.find_all(tag, cls)
        return r[0] if r else None

    def text(self):
        out = []
        for c in self.children:
            out.append(c if isinstance(c, str) else c.text())
        return "".join(out)


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(fragment: str) -> Node:
    p = TreeBuilder()
    p.feed(fragment)
    p.close()
    return p.root


# ---------------------------------------------------------------- md 方言
MD_ESC = {"*": r"\*", "`": r"\`", "[": r"\[", "]": r"\]", "_": r"\_", "\\": r"\\"}


def norm_ws(s: str) -> str:
    """HTML 会把源码中的连续空白折叠为一个空格，转换时保持一致。"""
    return re.sub(r"[ \t\r\n]+", " ", s)


def esc_md(s: str) -> str:
    return "".join(MD_ESC.get(ch, ch) for ch in s)


INLINE_TAGS = {"strong", "b", "em", "i", "code", "a", "br", "span", "sup", "sub"}


def _inline_md(n):
    """单个内联元素 -> markdown。"""
    if n.tag in ("strong", "b", "em", "i"):
        return "**" + to_md(n) + "**"
    if n.tag == "code":
        return "`" + n.text() + "`"
    if n.tag == "a":
        href = n.attrs.get("href", "")
        blank = n.attrs.get("target") == "_blank"
        return "[%s](%s%s)" % (to_md(n), href, " 'blank'" if blank else "")
    if n.tag == "br":
        return "\n"
    return to_md(n)


def to_md(node) -> str:
    """把内联 HTML 转成本方言 markdown。"""
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(esc_md(norm_ws(c)))
        else:
            out.append(_inline_md(c))
    return "".join(out).strip()


def inner_segments(node):
    """容器内容 -> 有序段落/列表分段（保留每个元素的属性），无法表达时返回 None。"""
    segs, buf = [], []

    def flush():
        if buf:
            t = "".join(buf).strip()
            if t:
                segs.append({"k": "p", "md": t, "bare": True})
            buf.clear()

    for c in node.children:
        if isinstance(c, str):
            buf.append(esc_md(norm_ws(c)))
            continue
        if c.tag == "p":
            flush()
            seg = {"k": "p", "md": to_md(c)}
            if c.attrs:
                seg["a"] = dict(c.attrs)
            segs.append(seg)
        elif c.tag in ("ul", "ol"):
            flush()
            seg = {"k": c.tag, "items": [to_md(li) for li in c.find_all("li")]}
            if c.attrs:
                seg["a"] = dict(c.attrs)
            segs.append(seg)
        elif c.tag in INLINE_TAGS:
            buf.append(_inline_md(c))
        elif c.tag == "div" and c.has("table-wrapper"):
            flush()
            segs.append({"k": "table", "wrap": True, "table": table_data(c.find("table"))})
        elif c.tag == "table":
            flush()
            segs.append({"k": "table", "wrap": False, "table": table_data(c)})
        elif c.tag == "div" and c.cls:
            return None
        else:
            flush()
            segs.append({"k": "p", "md": to_md(c)})
    flush()
    return segs


def md_inline(s: str) -> str:
    """本方言 markdown -> 内联 HTML。先转义实体字符，再做行内标记替换。"""
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\\(.)", lambda m: {"*": "&#42;", "`": "&#96;", "[": "&#91;",
                                     "]": "&#93;", "_": "&#95;", "\\": "&#92;",
                                     "&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(m.group(1), m.group(1)), s)
    s = re.sub(r"`([^`]+)`", lambda m: "<code>" + m.group(1) + "</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", lambda m: "<strong>" + m.group(1) + "</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^) ]+)(?: 'blank')?\)", _mk_link, s)
    return s.replace("\n", "<br>")


def _mk_link(m):
    href = m.group(2)
    target = ' target="_blank" rel="noopener"' if m.group(0).endswith("'blank')") else ""
    return f'<a href="{href}"{target}>{m.group(1)}</a>'


def md_block(md: str) -> str:
    """多行 md -> HTML：空行分段，段内单换行视为 <br>，'- ' 起头为无序列表。"""
    md = md.strip("\n")
    if not md.strip():
        return ""
    out = []
    for seg in re.split(r"\n\s*\n", md):
        seg = seg.strip("\n")
        if not seg.strip():
            continue
        lines = [l for l in seg.split("\n") if l.strip()]
        if lines and all(l.startswith("- ") for l in lines):
            items = "".join("<li>" + md_inline(l[2:].strip()) + "</li>" for l in lines)
            out.append('<ul style="padding-left:20px;margin-top:8px;'
                       'color:var(--color-text-light);">' + items + "</ul>")
        else:
            out.append("<p>" + md_inline("\n".join(lines)) + "</p>")
    return "\n".join(out)


def inner_html(node) -> str:
    return "".join(ser(c) for c in node.children)


def ser(n) -> str:
    if isinstance(n, str):
        return htmllib.escape(n, quote=False)
    a = "".join(f' {k}="{htmllib.escape(v, quote=True)}"' for k, v in n.attrs.items())
    if n.tag in VOID:
        return f"<{n.tag}{a}>"
    return f"<{n.tag}{a}>" + "".join(ser(c) for c in n.children) + f"</{n.tag}>"


SIMPLE_INLINE = {"strong", "b", "code", "a", "br", "em", "i"}


def cell_data(c):
    """单元格 -> markdown 字符串；含特殊标签时转为原始 HTML 对象。"""
    attrs = dict(c.attrs)
    exotic = any(isinstance(n, Node) and n.tag not in SIMPLE_INLINE for n in c.children)
    out = ("html:" + inner_html(c)) if exotic else to_md(c)
    if attrs:
        return {"md": out, "attrs": attrs}
    return out


def table_data(tbl):
    data = {"cls": tbl.cls or None,
            "head": [cell_data(th) for th in (tbl.find("thead").find_all("th")
                                              if tbl.find("thead") else [])],
            "rows": []}
    body = tbl.find("tbody") or tbl
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        data["rows"].append({
            "cls": tr.cls or None,
            "attrs": {k: v for k, v in tr.attrs.items() if k not in ("class",)},
            "cells": [cell_data(td) for td in tds],
        })
    return data


# ---------------------------------------------------------------- extract
def split_sections(h: str):
    """按 PAGE_IDS 顺序切出页面容器（section 或 div.hero）的外层 HTML。"""
    out = {}
    for pid in PAGE_IDS:
        m = re.search(r'<([a-zA-Z]+)[^>]*id="%s"' % re.escape(pid), h)
        if not m:
            raise SystemExit("找不到页面 " + pid)
        tag = m.group(1).lower()
        start = m.start()
        gt = h.find(">", m.end())
        depth, pos = 1, gt + 1
        open_re = re.compile(r"<%s\b" % tag, re.I)
        close = "</%s>" % tag
        while depth:
            no = open_re.search(h, pos)
            nc = h.find(close, pos)
            if nc == -1:
                raise SystemExit("页面未闭合 " + pid)
            if no and no.start() < nc:
                depth += 1
                pos = no.end()
            else:
                depth -= 1
                pos = nc + len(close)
        out[pid] = h[start:pos]
    return out


BOXES = {"tip-box": ("tip", "tip-title"), "warning-box": ("warning", "warn-title"),
         "highlight-box": ("highlight", "hl-title")}


def build_page(pid: str, outer: str) -> dict:
    root = parse(outer)
    sec = [n for n in root.children if isinstance(n, Node)][0]
    page = {"id": pid}
    h2 = sec.find("h2")
    if h2:
        num = h2.find("span", "sec-num")
        page["num"] = num.text().strip() if num else ""
        raw_title = h2.text().replace(page.get("num", ""), "", 1) if page.get("num") else h2.text()
        page["title"] = raw_title.strip()
    blocks = []
    for c in sec.children:
        if isinstance(c, str):
            continue
        if c.tag == "h2":
            continue
        if c.tag == "div" and c.has("section-toc"):
            items = [li.text().strip() for li in c.find_all("li")]
            page["toc"] = items
            continue
        if c.tag == "h3":
            blocks.append({"type": "h3", "id": c.attrs.get("id", ""), "text": c.text().strip()})
            continue
        if c.tag == "h4":
            blocks.append({"type": "h4", "id": c.attrs.get("id", ""), "text": c.text().strip()})
            continue
        if c.tag == "p":
            blk = {"type": "p", "md": to_md(c)}
            if c.attrs:
                blk["attrs"] = dict(c.attrs)
            blocks.append(blk)
            continue
        if c.tag in ("ul", "ol"):
            items = [to_md(li) for li in c.find_all("li")]
            blk = {"type": "checklist" if c.has("checklist") else "list",
                   "ordered": c.tag == "ol", "items": items}
            if c.attrs:
                blk["attrs"] = dict(c.attrs)
            blocks.append(blk)
            continue
        if c.tag == "div" and c.has("info-card"):
            blk = {"type": "card"}
            t = c.find("div", "card-title")
            if t:
                blk["title"] = to_md(t)
            items = []
            for x in c.children:
                if isinstance(x, str):
                    if x.strip():
                        items.append({"kind": "raw", "html": htmllib.escape(x.strip(), quote=False)})
                    continue
                if x is t or x.has("card-title"):
                    continue
                if x.has("table-wrapper"):
                    items.append({"kind": "table", "wrap": True,
                                  "table": table_data(x.find("table"))})
                elif x.tag == "table":
                    items.append({"kind": "table", "wrap": False,
                                  "table": table_data(x)})
                else:
                    items.append({"kind": "raw", "html": ser(x)})
            blk["items"] = items
            blocks.append(blk)
            continue
        if c.tag == "div" and any(c.has(k) for k in BOXES):
            key = next(k for k in BOXES if c.has(k))
            variant, title_cls = BOXES[key]
            blk = {"type": "callout", "variant": variant}
            t = c.find("div", title_cls) or c.find("div", None)
            if t and (title_cls in t.cls or "title" in t.cls):
                blk["title"] = to_md(t)
            body = Node("#body")
            skipped = False
            for x in c.children:
                if isinstance(x, Node) and x is t and not skipped:
                    skipped = True
                    continue
                body.children.append(x)
            segs = inner_segments(body)
            if segs is None:
                blk["html"] = inner_html(c)
            else:
                blk["body"] = segs
            blocks.append(blk)
            continue
        if c.tag == "div" and c.has("flow-steps"):
            steps = []
            for st in c.find_all("div", "flow-step"):
                d = {"num": "", "label": "", "desc": ""}
                for part, key in (("step-num", "num"), ("step-label", "label"), ("step-desc", "desc")):
                    n = st.find("div", part)
                    if n:
                        d[key] = inner_html(n).strip()
                steps.append(d)
            blocks.append({"type": "flow", "steps": steps})
            continue
        if c.tag == "div" and c.has("pipeline"):
            stages = []
            for st in c.find_all("div", "pipeline-stage"):
                d = {"num": "", "title": "", "detail": ""}
                for part, key in (("pipeline-num", "num"), ("pipeline-title", "title"),
                                  ("pipeline-detail", "detail")):
                    n = st.find("div", part)
                    if n:
                        d[key] = inner_html(n).strip()
                stages.append(d)
            blocks.append({"type": "pipeline", "steps": stages})
            continue
        if c.tag == "div" and c.has("table-wrapper"):
            blocks.append({"type": "table", "table": table_data(c.find("table"))})
            continue
        if c.tag == "table":
            blocks.append({"type": "table", "table": table_data(c)})
            continue
        # 其余一律保留原始 HTML
        blocks.append({"type": "raw", "html": ser(c), "note": c.cls or c.tag})
    page["nav_title"] = "指南首页" if pid == "cover" else page.get("title", "")
    meta = NAV_DEFAULT.get(pid, {})
    page["nav_icon"] = meta.get("icon")
    page["nav_style"] = meta.get("num_style")
    page["nav_group"] = NAV_GROUPS.get(pid)
    page["blocks"] = blocks
    return page


# ---------------------------------------------------------------- nav meta
NAV_GROUPS = {"sec1": "执行层面", "sec8": "其他资料"}
NAV_DEFAULT = {
    "cover": {"icon": "⌂", "num_style": "muted", "group": None},
    "sec0": {"icon": "✦", "num_style": "muted", "group": None},
    "sec-intro": {"icon": None, "num_style": "accent", "group": None},
}


def cmd_extract():
    h = SRC.read_text(encoding="utf-8")
    sections = split_sections(h)
    CONTENT.mkdir(exist_ok=True)
    BUILD.mkdir(exist_ok=True)

    # 侧栏导航原样取出来做对照，稍后由 render 重建
    nav_m = re.search(r'(<nav class="sidebar-nav">)(.*?)(</nav>)', h, re.S)
    nav_outer = nav_m.group(2)

    # 外壳：把 11 个 section 与 nav 内容替换为占位符
    shell = h
    for pid, outer in sections.items():
        shell = shell.replace(outer, "<!--PAGE:%s-->" % pid, 1)
    shell = shell.replace(nav_m.group(1) + nav_outer + nav_m.group(3),
                          '<nav class="sidebar-nav">\n<!--NAV-->\n  </nav>', 1)
    SHELL.write_text(shell, encoding="utf-8")

    pages = []
    for pid in PAGE_IDS:
        pg = build_page(pid, sections[pid])
        pages.append(pg)
        (CONTENT / f"{pid}.json").write_text(
            json.dumps(pg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK extract  pages={len(pages)}  shell={len(shell)}B  content={CONTENT}")


# ---------------------------------------------------------------- render
def render_nav(pages):
    out, cur_group = [], None
    for pg in pages:
        g = pg.get("nav_group")
        if g and g != cur_group:
            out.append(f'    <div class="nav-section-title">{g}</div>')
            cur_group = g
        icon = pg.get("nav_icon")
        style = pg.get("nav_style")
        num = pg.get("num", "")
        if icon:
            badge = f'<span class="nav-num" style="background:rgba(255,255,255,0.18);color:#fff;">{icon}</span>'
        elif style == "accent":
            badge = f'<span class="nav-num" style="background:var(--color-accent);color:var(--color-primary);">{num}</span>'
        else:
            badge = f'<span class="nav-num">{num}</span>' if num else ""
        subs = [b for b in pg["blocks"] if b["type"] == "h3"]
        label = pg.get("nav_title") or pg.get("title", "")
        if subs:
            out.append('    <div class="nav-item has-children">')
            out.append(f'      <a href="#{pg["id"]}" class="nav-link">{badge}{label}</a>')
            out.append(f'      <button type="button" class="nav-caret" aria-expanded="false" '
                       f'aria-label="展开/收起「{num}{label}」的二级目录"><span>▸</span></button>')
            out.append('      <div class="nav-children">')
            for s in subs:
                txt = s["text"]
                m = re.match(r"^((?:\d+|[一二三四五六七八九十]+))[\.\、]\s*(\d+)?\s*(.*)$", txt)
                subnum = ""
                if m and re.match(r"^\d+\.\d+", txt):
                    subnum = txt.split()[0]
                    rest = txt[len(subnum):].strip()
                    out.append(f'        <a href="#{s["id"]}" class="nav-sub">'
                               f'<span class="sub-num">{subnum}</span>{rest}</a>')
                    continue
                if re.match(r"^[一二三四五六七八九十]+、", txt):
                    rest = re.sub(r"^[一二三四五六七八九十]+、\s*", "", txt)
                    out.append(f'        <a href="#{s["id"]}" class="nav-sub">{rest}</a>')
                    continue
                out.append(f'        <a href="#{s["id"]}" class="nav-sub">{txt}</a>')
            out.append('      </div>')
            out.append('    </div>')
        else:
            out.append('    <div class="nav-item">')
            out.append(f'      <a href="#{pg["id"]}" class="nav-link">{badge}{label}</a>')
            out.append('    </div>')
    return "\n".join(out)


def _attrs(d):
    return "".join(f' {k}="{htmllib.escape(str(v), quote=True)}"' for k, v in (d or {}).items())


def render_segments(segs, indent="      "):
    out = []
    for sg in segs:
        a = _attrs(sg.get("a"))
        if sg["k"] == "table":
            tbl = render_table(sg["table"])
            out.append(f'{indent}<div class="table-wrapper">\n' + tbl + "\n" + indent + "</div>"
                       if sg.get("wrap") else tbl)
        elif sg["k"] == "p" and sg.get("bare"):
            out.append(f"{indent}{md_inline(sg['md'])}")
        elif sg["k"] == "p":
            out.append(f"{indent}<p{a}>{md_inline(sg['md'])}</p>")
        else:
            items = "".join(f"<li>{md_inline(x)}</li>" for x in sg["items"])
            out.append(f"{indent}<{sg['k']}{a}>{items}</{sg['k']}>")
    return "\n".join(out)


def render_cell(c, tag):
    attrs = ""
    if isinstance(c, dict):
        attrs = "".join(f' {k}="{htmllib.escape(str(v), quote=True)}"'
                        for k, v in (c.get("attrs") or {}).items())
        raw = c.get("md", "")
        inner = raw[5:] if raw.startswith("html:") else md_inline(raw)
    else:
        inner = c[5:] if c.startswith("html:") else md_inline(c)
    return f"<{tag}{attrs}>{inner}</{tag}>"


def render_table(tbl):
    head = "".join(render_cell(x, "th") for x in tbl.get("head", []))
    thead = f"          <tr>{head}</tr>\n" if head else ""
    rows = []
    for r in tbl.get("rows", []):
        if isinstance(r, dict):
            tr_attrs = ""
            if r.get("cls"):
                tr_attrs += f' class="{r["cls"]}"'
            tr_attrs += "".join(f' {k}="{htmllib.escape(str(v), quote=True)}"'
                                for k, v in (r.get("attrs") or {}).items())
            cells = "".join(render_cell(c, "td") for c in r["cells"])
        else:
            tr_attrs = ""
            cells = "".join(render_cell(c, "td") for c in r)
        rows.append(f"          <tr{tr_attrs}>{cells}</tr>")
    body = "\n".join(rows)
    tcls = f' class="{tbl["cls"]}"' if tbl.get("cls") else ""
    return (f"        <table{tcls}>\n" +
            ("        <thead>\n" + thead + "        </thead>\n" if head else "") +
            "        <tbody>\n" + body + "\n        </tbody>\n        </table>")


def render_block(b):
    t = b["type"]
    if t == "h3":
        return f'    <h3 id="{b["id"]}">{b["text"]}</h3>'
    if t == "h4":
        return f'    <h4 id="{b["id"]}">{b["text"]}</h4>'
    if t == "p":
        return "    <p" + _attrs(b.get("attrs")) + ">" + md_inline(b["md"]) + "</p>"
    if t == "list":
        tag = "ol" if b.get("ordered") else "ul"
        items = "".join(f"<li>{md_inline(x)}</li>" for x in b["items"])
        return f"    <{tag}{_attrs(b.get('attrs'))}>{items}</{tag}>"
    if t == "checklist":
        items = "".join(f"<li>{md_inline(x)}</li>" for x in b["items"])
        return f'    <ul{_attrs(b.get("attrs"))}>{items}</ul>'
    if t == "card":
        inner = ""
        if b.get("title"):
            inner += f'      <div class="card-title">{md_inline(b["title"])}</div>\n'
        for it in b.get("items", []):
            if it["kind"] == "table":
                tbl = render_table(it["table"])
                if it.get("wrap", True):
                    inner += '      <div class="table-wrapper">\n' + tbl + "\n      </div>\n"
                else:
                    inner += tbl + "\n"
            else:
                inner += "      " + it["html"] + "\n"
        return '    <div class="info-card">\n' + inner + "    </div>"
    if t == "table":
        return '    <div class="table-wrapper">\n' + render_table(b["table"]) + "\n    </div>"
    if t == "callout":
        cls = {"tip": "tip-box", "warning": "warning-box", "highlight": "highlight-box"}[b["variant"]]
        tcls = {"tip": "tip-title", "warning": "warn-title", "highlight": "hl-title"}[b["variant"]]
        inner = ""
        if b.get("title"):
            inner += f'      <div class="{tcls}">{md_inline(b["title"])}</div>\n'
        if b.get("html"):
            inner += "      " + b["html"] + "\n"
        elif b.get("body"):
            inner += render_segments(b["body"]) + "\n"
        return f'    <div class="{cls}">\n' + inner + "    </div>"
    if t == "flow":
        items = []
        for s in b.get("steps", []):
            items.append('      <div class="flow-step">\n'
                         f'        <div class="step-num">{s["num"]}</div>\n'
                         f'        <div class="step-label">{s["label"]}</div>\n'
                         f'        <div class="step-desc">{s["desc"]}</div>\n'
                         "      </div>")
        return '    <div class="flow-steps">\n' + "\n".join(items) + "\n    </div>"
    if t == "pipeline":
        items = []
        for i, s in enumerate(b.get("steps", [])):
            if i:
                items.append('      <div class="pipeline-arrow">→</div>')
            items.append('      <div class="pipeline-stage">\n'
                         f'        <div class="pipeline-num">{s["num"]}</div>\n'
                         '        <div class="pipeline-body">\n'
                         f'          <div class="pipeline-title">{s["title"]}</div>\n'
                         f'          <div class="pipeline-detail">{s["detail"]}</div>\n'
                         "        </div>\n"
                         "      </div>")
        return '    <div class="pipeline">\n' + "\n".join(items) + "\n    </div>"
    if t == "raw":
        return "    " + b["html"]
    raise SystemExit("未知块类型 " + t)


def render_page(pg):
    if pg["id"] == "cover":
        tag, cls, attrs = "div", "hero active", ""
    else:
        tag, cls, attrs = "section", "section", ""
    out = [f'<{tag} class="{cls}" id="{pg["id"]}"{attrs}>']
    num = pg.get("num", "")
    badge = f'<span class="sec-num">{num}</span>' if num else ""
    if pg.get("title"):
        out.append(f'    <h2>{badge}{pg["title"]}</h2>')
    if pg.get("toc"):
        lis = "".join(f"          <li>{x}</li>\n" for x in pg["toc"])
        out.append('    <div class="section-toc">\n      <div class="toc-title">本节内容</div>\n'
                   '      <ol>\n' + lis + '      </ol>\n    </div>')
    for b in pg["blocks"]:
        out.append(render_block(b))
    out.append(f"</{tag}>")
    return "\n".join(out)


def load_pages():
    pages = []
    for pid in PAGE_IDS:
        pages.append(json.loads((CONTENT / f"{pid}.json").read_text(encoding="utf-8")))
    return pages


def cmd_render():
    shell = SHELL.read_text(encoding="utf-8")
    pages = load_pages()
    shell = shell.replace("<!--NAV-->", render_nav(pages), 1)
    for pg in pages:
        shell = shell.replace("<!--PAGE:%s-->" % pg["id"], render_page(pg), 1)
    OUT.write_text(shell, encoding="utf-8")
    print(f"OK render  -> {OUT}  {len(shell)}B")


def visible_text(fragment: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", "", htmllib.unescape(t))


def cmd_compare():
    a = SRC.read_text(encoding="utf-8")
    b = OUT.read_text(encoding="utf-8")
    sa, sb = split_sections(a), split_sections(b)
    bad = 0
    for pid in PAGE_IDS:
        ta, tb = visible_text(sa[pid]), visible_text(sb[pid])
        if ta == tb:
            print(f"  {pid:10s} 文字一致  {len(ta)} 字")
        else:
            bad += 1
            import difflib
            d = list(difflib.unified_diff([ta[i:i + 60] for i in range(0, len(ta), 60)],
                                          [tb[i:i + 60] for i in range(0, len(tb), 60)], lineterm="", n=0))
            print(f"  {pid:10s} 差异 {len(ta)} -> {len(tb)}")
            for line in d[2:8]:
                print("      " + line[:100])
    print("RESULT:", "ALL_MATCH" if bad == 0 else f"{bad} 页有差异")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "render"
    {"extract": cmd_extract, "render": cmd_render, "compare": cmd_compare}[cmd]()
