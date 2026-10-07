"""Wiki 工作台的纯展示逻辑；页面链接由 Markdown 语法派生，不进入图谱。"""
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from uuid import UUID

# Wiki 页面类型（正式存储为 Markdown + JSON frontmatter）。
KINDS = ("concept", "entity", "topic", "synthesis", "source_summary")
STATUSES = ("active", "disputed", "outdated")


def safe_destination(value):
    """只接受 piece://wiki/<UUID> 页面内链与无凭据的 http/https 外链。"""
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "piece" and parsed.netloc == "wiki" and not parsed.query and not parsed.fragment:
            ident = str(UUID(parsed.path[1:]))
            if parsed.path[1:].lower() == ident:
                return ("wiki", ident)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            parsed.port
            return ("external", value)
    except (ValueError, TypeError):
        pass
    return None


class _SafeWikiHTML(HTMLParser):
    tags = set("p br hr h1 h2 h3 h4 h5 h6 em strong del blockquote ul ol li pre code table thead tbody tr th td a span div math semantics mrow mi mn mo mtext mspace mfrac msqrt mroot msup msub msubsup munder mover munderover mtable mtr mtd annotation".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            return
        attributes = ""
        if tag == "a":
            destination = safe_destination(dict(attrs).get("href"))
            if destination:
                kind, value = destination
                if kind == "wiki":
                    attributes = f' href="#wiki/{value}" data-wiki-id="{value}"'
                else:
                    attributes = f' href="{escape(value, quote=True)}" target="_blank" rel="noopener noreferrer"'
        self.parts.append(f"<{tag}{attributes}>")

    def handle_endtag(self, tag):
        if tag in self.tags:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data))


def safe_wiki_html(rendered):
    parser = _SafeWikiHTML()
    parser.feed(rendered)
    return "".join(parser.parts)
