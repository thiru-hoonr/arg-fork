"""Module edges for XSLT: no tree-sitter grammar; reuses the module's own approach for XSLT (stdlib expat, as
core-module/extract.py xslt_functions). import = xsl:import / xsl:include (@href); call = xsl:call-template (@name)."""
from pathlib import Path
import xml.parsers.expat as expat
LABEL = "stdlib expat (module's XSLT approach, no grammar): xsl:import/xsl:include, xsl:call-template"
XSL = 'http://www.w3.org/1999/XSL/Transform'

def edges(root, files):
    out = []
    for rel in files:
        p = expat.ParserCreate(namespace_separator='|'); rows = []
        def start(tag, attrs, p=p, rows=rows, rel=rel):
            ns, _, local = tag.rpartition('|')
            if ns != XSL: return
            ln = p.CurrentLineNumber
            if local in ('import', 'include'):
                rows.append(dict(e='import', file=rel, line=ln, name=attrs.get('href', '')))
            elif local == 'call-template' and attrs.get('name'):
                rows.append(dict(e='call', file=rel, line=ln, start=ln, name=' '.join(attrs['name'].split())))
        p.StartElementHandler = start
        try:
            p.Parse((Path(root) / rel).read_bytes(), True)
        except Exception:
            continue
        out += rows
    return out
