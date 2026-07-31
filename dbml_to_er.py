#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbml_to_er.py - turn a DBML schema into an *untangled* ER diagram.

  python dbml_to_er.py schema.dbml

Outputs (next to the input, or use -o):
  <name>.html  - interactive viewer: click a table to highlight its links,
                 search, zoom, drag-to-edit, export SVG.
  <name>.svg   - static untangled diagram (use --no-svg to skip).

The layout is force-directed: linked tables are pulled side-by-side and
vertically aligned by the row their key sits on, so relationship lines stay
short and mostly horizontal. Multiple random seeds are tried and the one with
the fewest line crossings wins.
"""
import re, json, html, math, random, argparse, os, sys

# ----------------------------------------------------------------------------
# geometry of a table card (matches the original visual style)
# ----------------------------------------------------------------------------
W_TABLE   = 240
ROW_H     = 26
COL0_Y    = 61          # baseline of first column, relative to table top
HEADER_H  = 38

def table_height(ncols):
    return 58 + ROW_H * max(ncols, 1)

def col_offset(i):      # y of column i baseline, relative to table top
    return COL0_Y + i * ROW_H

# ----------------------------------------------------------------------------
# DBML parsing
# ----------------------------------------------------------------------------
def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")

def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)   # block comments
    out = []
    for line in text.splitlines():
        # drop // line comments (naive; DBML rarely has // inside strings)
        i = line.find("//")
        if i >= 0:
            line = line[:i]
        out.append(line)
    return "\n".join(out)

def unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] in '"`' and s[-1] == s[0]:
        return s[1:-1]
    return s

def short_name(tok):
    """schema.table or "schema"."table" -> table identifier used in refs."""
    tok = tok.strip()
    # split on dots that are not inside quotes
    parts = re.findall(r'"[^"]*"|`[^`]*`|[^.]+', tok)
    parts = [unquote(p) for p in parts]
    return parts[-1] if parts else tok

CARD = {">": ("N", "1"), "<": ("1", "N"), "-": ("1", "1"), "<>": ("N", "N")}

def parse_ref_endpoint(tok):
    tok = tok.strip()
    # Table.Col  or  Table.(Col1, Col2)
    m = re.match(r'(.+?)\.\(?\s*([^,\)\s]+)', tok)
    if not m:
        return None, None
    return short_name(m.group(1)), unquote(m.group(2))

def add_ref(refs, a, ac, b, bc, op):
    fc, tc = CARD.get(op, ("N", "1"))
    if a and b and ac and bc:
        refs.append((a, ac, b, bc, fc, tc))

def parse_dbml(text):
    text = strip_comments(text)
    lines = text.split("\n")
    tables = {}          # name -> list of (col, type, pk)
    order = []           # table name order
    refs = []            # (ftab, fcol, ttab, tcol, fcard, tcard)

    i, n = 0, len(lines)
    SKIP = ("project", "enum", "tablegroup", "note", "indexes")
    while i < n:
        line = lines[i].strip()
        i += 1
        if not line:
            continue

        low = line.lower()

        # ---- Ref (short or block) ----
        if low.startswith("ref"):
            mref = re.match(r'ref\b[^:{]*:?\s*(.*)$', line, re.I)
            body = mref.group(1).strip() if mref else ""
            if "{" in line and ":" not in line.split("{")[0]:
                # block form: collect until }
                block = line[line.index("{") + 1:]
                while "}" not in block and i < n:
                    block += " " + lines[i]; i += 1
                block = block.split("}")[0]
                for part in re.split(r'[\n,]', block):
                    mm = re.match(r'\s*(.+?)\s*([<>-]{1,2})\s*(.+?)\s*$', part)
                    if mm:
                        a, ac = parse_ref_endpoint(mm.group(1))
                        b, bc = parse_ref_endpoint(mm.group(3))
                        add_ref(refs, a, ac, b, bc, mm.group(2))
                continue
            mm = re.match(r'(.+?)\s*([<>-]{1,2})\s*(.+?)\s*$', body)
            if mm:
                a, ac = parse_ref_endpoint(mm.group(1))
                b, bc = parse_ref_endpoint(mm.group(3))
                add_ref(refs, a, ac, b, bc, mm.group(2))
            continue

        # ---- skip non-table blocks ----
        if any(low.startswith(k) for k in SKIP):
            if "{" in line:
                depth = line.count("{") - line.count("}")
                while depth > 0 and i < n:
                    depth += lines[i].count("{") - lines[i].count("}"); i += 1
            continue

        # ---- Table ----
        mt = re.match(r'table\s+(.+?)\s*(\{)?\s*$', line, re.I)
        if low.startswith("table") and mt:
            decl = mt.group(1)
            decl = re.sub(r'\s+as\s+\w+\s*$', '', decl, flags=re.I)  # drop alias
            tname = short_name(decl)
            if "{" not in line:                       # brace on next line(s)
                while i < n and "{" not in lines[i]:
                    i += 1
                if i < n:
                    i += 1
            cols = []
            while i < n and "}" not in lines[i]:
                cl = lines[i].strip(); i += 1
                if not cl or cl.lower().startswith("indexes") or cl.startswith("("):
                    continue
                if cl.lower().startswith("note"):
                    continue
                # column:  name  type  [settings]
                cm = re.match(r'("[^"]+"|`[^`]+`|[^\s\[]+)\s+([^\[\s]+(?:\([^)]*\))?)\s*(\[.*\])?', cl)
                if not cm:
                    cm = re.match(r'("[^"]+"|`[^`]+`|\S+)\s*(\[.*\])?', cl)
                    if not cm:
                        continue
                    cname = unquote(cm.group(1)); ctype = ""; settings = cm.group(2) or ""
                else:
                    cname = unquote(cm.group(1)); ctype = cm.group(2); settings = cm.group(3) or ""
                pk = bool(re.search(r'\b(pk|primary key)\b', settings, re.I))
                im = re.search(r'ref:\s*([<>-]{1,2})\s*([^\],]+)', settings, re.I)
                if im:
                    b, bc = parse_ref_endpoint(im.group(2))
                    add_ref(refs, tname, cname, b, bc, im.group(1))
                cols.append((cname, ctype, pk))
            if i < n:
                i += 1                                # consume closing }
            if tname not in tables:
                order.append(tname)
            tables[tname] = cols
            continue

    # refs are returned unfiltered: a ref may point at a table defined in another
    # .dbml, and it comes alive once that file is merged in (see merge_files)
    return order, tables, refs


def merge_files(paths, quiet=False):
    """Parse several .dbml files into one schema. Later files win: a table
    redefined later replaces the earlier definition; refs are de-duplicated."""
    order, tables, refs, seen = [], {}, [], set()
    for path in paths:
        o, t, r = parse_dbml(read_text(path))
        added = updated = 0
        for n in o:
            if n in tables:
                updated += 1
            else:
                order.append(n); added += 1
            tables[n] = t[n]                     # newer definition wins
        new_refs = 0
        for ref in r:
            key = ref[:4]
            if key not in seen:
                seen.add(key); refs.append(ref); new_refs += 1
        if not quiet:
            print(f"  {os.path.basename(path)}: +{added} new, {updated} updated, +{new_refs} refs")
    refs = [r for r in refs if r[0] in tables and r[2] in tables]
    return order, tables, refs

# ----------------------------------------------------------------------------
# layout (force-directed, multi-seed, pick fewest crossings)
# ----------------------------------------------------------------------------
def layout(order, tables, refs, seeds=10, verbose=True):
    W = {n: W_TABLE for n in order}
    H = {n: table_height(len(tables[n])) for n in order}
    col_index = {n: {c[0]: idx for idx, c in enumerate(tables[n])} for n in order}

    # resolve each ref to anchor offsets (fallback: middle of table)
    E = []
    for ftab, fcol, ttab, tcol, fc, tc in refs:
        if ftab == ttab:
            continue
        fi = col_index[ftab].get(fcol)
        ti = col_index[ttab].get(tcol)
        foff = col_offset(fi) if fi is not None else H[ftab] / 2
        toff = col_offset(ti) if ti is not None else H[ttab] / 2
        E.append(dict(f=ftab, t=ttab, foff=foff, toff=toff, fc=fc, tc=tc,
                      fcol=fcol, tcol=tcol))

    deg = {n: 0 for n in order}
    for e in E:
        deg[e["f"]] += 1; deg[e["t"]] += 1
    connected = [n for n in order if deg[n] > 0]
    isolated  = [n for n in order if deg[n] == 0]

    total_area = sum(W[n] * H[n] for n in connected) or 1
    side = math.sqrt(total_area) * 1.5
    GAP_X, PAD_X, PAD_Y = 130.0, 90.0, 46.0
    MX, MY = 80.0, 40.0
    cx, cy = {}, {}

    def repel(temp):
        fx = {n: 0.0 for n in connected}; fy = {n: 0.0 for n in connected}
        cl = connected
        for a in range(len(cl)):
            na = cl[a]
            for b in range(a + 1, len(cl)):
                nb = cl[b]
                dx = cx[na] - cx[nb]; dy = cy[na] - cy[nb]
                if dx == 0 and dy == 0:
                    dx = random.uniform(-1, 1); dy = random.uniform(-1, 1)
                minx = (W[na] + W[nb]) / 2 + PAD_X
                miny = (H[na] + H[nb]) / 2 + PAD_Y
                ox = minx - abs(dx); oy = miny - abs(dy)
                if ox > 0 and oy > 0:
                    if ox < oy:
                        p = ox * 0.5 * (1 if dx >= 0 else -1); fx[na] += p; fx[nb] -= p
                    else:
                        p = oy * 0.5 * (1 if dy >= 0 else -1); fy[na] += p; fy[nb] -= p
                dist2 = dx * dx + dy * dy + 1.0
                f = 9000.0 / dist2; inv = 1.0 / math.sqrt(dist2)
                fx[na] += dx * inv * f; fy[na] += dy * inv * f
                fx[nb] -= dx * inv * f; fy[nb] -= dy * inv * f
        return fx, fy

    def attract(fx, fy):
        for e in E:
            f, t = e["f"], e["t"]
            af = (cy[f] - H[f] / 2) + e["foff"]; at = (cy[t] - H[t] / 2) + e["toff"]
            v = at - af; fy[f] += 0.08 * v; fy[t] -= 0.08 * v
            sx = cx[t] - cx[f]; sep = (W[f] + W[t]) / 2 + GAP_X
            s = 1 if sx >= 0 else -1; err = abs(sx) - sep
            fx[f] += 0.05 * s * err; fx[t] -= 0.05 * s * err

    def overlap_removal():
        cl = connected
        for _ in range(600):
            moved = False
            for a in range(len(cl)):
                na = cl[a]
                for b in range(a + 1, len(cl)):
                    nb = cl[b]
                    dx = cx[na] - cx[nb]; dy = cy[na] - cy[nb]
                    ox = (W[na] + W[nb]) / 2 + MX - abs(dx)
                    oy = (H[na] + H[nb]) / 2 + MY - abs(dy)
                    if ox > 0 and oy > 0:
                        moved = True
                        if ox < oy:
                            sh = ox / 2 * (1 if dx >= 0 else -1); cx[na] += sh; cx[nb] -= sh
                        else:
                            sh = oy / 2 * (1 if dy >= 0 else -1); cy[na] += sh; cy[nb] -= sh
            if not moved:
                break

    def metric():
        segs = []
        for e in E:
            f, t = e["f"], e["t"]
            fcx = cx[f]; tcx = cx[t]
            fay = (cy[f] - H[f] / 2) + e["foff"]; tay = (cy[t] - H[t] / 2) + e["toff"]
            if tcx >= fcx:
                p0 = (cx[f] + W[f] / 2, fay); p3 = (cx[t] - W[t] / 2, tay)
            else:
                p0 = (cx[f] - W[f] / 2, fay); p3 = (cx[t] + W[t] / 2, tay)
            segs.append((p0, p3, f, t))
        def ccw(A, B, C): return (C[1]-A[1])*(B[0]-A[0]) - (B[1]-A[1])*(C[0]-A[0])
        c = 0; L = 0.0
        for i2 in range(len(segs)):
            a1, a2, fa, ta = segs[i2]; L += math.hypot(a2[0]-a1[0], a2[1]-a1[1])
            for j in range(i2 + 1, len(segs)):
                b1, b2, fb, tb = segs[j]
                if len({fa, ta, fb, tb}) < 4: continue
                d1 = ccw(a1, a2, b1); d2 = ccw(a1, a2, b2)
                d3 = ccw(b1, b2, a1); d4 = ccw(b1, b2, a2)
                if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)): c += 1
        return c, L

    def run(seed):
        random.seed(seed)
        for n in connected:
            cx[n] = random.uniform(0, side); cy[n] = random.uniform(0, side)
        temp = side * 0.1
        for _ in range(1400):
            fx, fy = repel(temp); attract(fx, fy)
            for n in connected:
                dx, dy = fx[n], fy[n]; d = math.hypot(dx, dy)
                if d > temp: dx *= temp / d; dy *= temp / d
                cx[n] += dx; cy[n] += dy
            temp = max(temp * 0.997, 8.0)
        overlap_removal()
        return metric()

    best = None
    if connected:
        for seed in range(seeds):
            c, L = run(seed)
            if verbose: print(f"  seed {seed}: crossings={c} length={L:.0f}")
            if best is None or (c, L) < best[0]:
                best = ((c, L), {n: cx[n] for n in connected}, {n: cy[n] for n in connected})
        cx, cy = best[1], best[2]

    # top-left coords + normalize
    newx, newy = {}, {}
    for n in connected:
        newx[n] = cx[n] - W[n] / 2; newy[n] = cy[n] - H[n] / 2
    if connected:
        minx = min(newx[n] for n in connected); miny = min(newy[n] for n in connected)
        maxx = max(newx[n] + W[n] for n in connected); maxy = max(newy[n] + H[n] for n in connected)
    else:
        minx = miny = 0; maxx = maxy = side
    MARGIN = 80
    for n in connected:
        newx[n] += MARGIN - minx; newy[n] += MARGIN - miny
    maxx += MARGIN - minx; maxy += MARGIN - miny

    # isolated tables in a tidy row beneath the connected block
    iso_y = maxy + 160; ix = MARGIN; row_h = 0
    content_w = max(maxx + MARGIN, side)
    for n in isolated:
        if ix + W[n] > content_w:
            ix = MARGIN; iso_y += row_h + 80; row_h = 0
        newx[n] = ix; newy[n] = iso_y
        ix += W[n] + 80; row_h = max(row_h, H[n])

    CANVAS_W = int(math.ceil(max(newx[n] + W[n] for n in order) + MARGIN))
    CANVAS_H = int(math.ceil(max(newy[n] + H[n] for n in order) + MARGIN))

    info = best[0] if best else (0, 0)
    return dict(x=newx, y=newy, W=W, H=H, E=E, connected=connected,
                isolated=isolated, cw=CANVAS_W, ch=CANVAS_H,
                crossings=info[0], length=info[1], col_index=col_index)

# ----------------------------------------------------------------------------
# SVG generation
# ----------------------------------------------------------------------------
def esc(s): return html.escape(str(s), quote=True)

def fk_columns(refs):
    s = set()
    for ftab, fcol, ttab, tcol, *_ in refs:
        s.add((ftab, fcol)); s.add((ttab, tcol))
    return s

def table_markup(name, cols, x, y, fkset, pad="    "):
    h = table_height(len(cols))
    p = [f'{pad}<g class="table draggable" data-table="{esc(name)}" transform="translate({x:.0f},{y:.0f})">']
    p.append(f'{pad}  <rect class="table-bg" x="0" y="0" width="{W_TABLE}" height="{h}" fill="#2a2a2a" stroke="#3498db" stroke-width="2" rx="8" ry="8" filter="url(#shadow)"/>')
    p.append(f'{pad}  <rect class="table-header-bg" x="0" y="0" width="{W_TABLE}" height="{HEADER_H}" fill="#3498db" rx="8" ry="8"/>')
    p.append(f'{pad}  <rect x="0" y="{HEADER_H-8}" width="{W_TABLE}" height="8" fill="#3498db"/>')
    p.append(f'{pad}  <text x="{W_TABLE//2}" y="24" text-anchor="middle" class="table-header">{esc(name)}</text>')
    for i, (cname, ctype, pk) in enumerate(cols):
        by = col_offset(i)
        if i % 2 == 1:
            p.append(f'{pad}  <rect x="2" y="{by-15}" width="{W_TABLE-4}" height="{ROW_H}" fill="rgba(255,255,255,0.02)" rx="2"/>')
        p.append(f'{pad}  <g class="column" data-table="{esc(name)}" data-column="{esc(cname)}">')
        if pk:
            p.append(f'{pad}    <text x="10" y="{by}" class="pk-icon">PK</text>')
            p.append(f'{pad}    <text x="30" y="{by}" class="column-name">{esc(cname)}</text>')
        elif (name, cname) in fkset:
            p.append(f'{pad}    <text x="10" y="{by}" class="fk-icon">&#128279;</text>')
            p.append(f'{pad}    <text x="28" y="{by}" class="column-name">{esc(cname)}</text>')
        else:
            p.append(f'{pad}    <text x="14" y="{by}" class="column-name">{esc(cname)}</text>')
        if ctype:
            p.append(f'{pad}    <text x="{W_TABLE-10}" y="{by}" text-anchor="end" class="column-type">{esc(ctype)}</text>')
        p.append(f'{pad}  </g>')
        if i < len(cols) - 1:
            p.append(f'{pad}  <line x1="8" y1="{by+12}" x2="{W_TABLE-8}" y2="{by+12}" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>')
    p.append(f'{pad}</g>')
    return "\n".join(p)

def route(e, X, Y, W, H):
    f, t = e["f"], e["t"]
    fx, fy = X[f], Y[f]; tx, ty = X[t], Y[t]
    fcx = fx + W[f] / 2; tcx = tx + W[t] / 2
    fay = fy + e["foff"]; tay = ty + e["toff"]
    if tcx >= fcx:
        p0 = (fx + W[f], fay); p3 = (tx, tay); fr, el_ = True, True
    else:
        p0 = (fx, fay); p3 = (tx + W[t], tay); fr, el_ = False, False
    co = min(max(abs(p3[0] - p0[0]) * 0.5, 45), 160)
    c1 = (p0[0] + co if fr else p0[0] - co, p0[1])
    c2 = (p3[0] - co if el_ else p3[0] + co, p3[1])
    d = f"M {p0[0]:.0f} {p0[1]:.0f} C {c1[0]:.0f} {c1[1]:.0f}, {c2[0]:.0f} {c2[1]:.0f}, {p3[0]:.0f} {p3[1]:.0f}"
    fb = (p0[0] + 16 if fr else p0[0] - 40, p0[1] - 24)
    tb = (p3[0] - 40 if el_ else p3[0] + 16, p3[1] - 24)
    return dict(d=d, fbx=fb[0], fby=fb[1], ftx=fb[0]+12, fty=p0[1]-10,
                tbx=tb[0], tby=tb[1], ttx=tb[0]+12, tty=p3[1]-10)

DEFS = '''  <defs>
    <style>
      .table-header { font-family: 'Segoe UI','SF Pro Display',Arial,sans-serif; font-size:13px; font-weight:600; fill:white; }
      .column-name { font-family: 'Segoe UI','SF Pro Display',Arial,sans-serif; font-size:11px; fill:#e0e0e0; }
      .column-type { font-family: 'Segoe UI','SF Pro Display',Arial,sans-serif; font-size:10px; fill:#888; }
      .pk-icon { font-family:'Segoe UI',Arial,sans-serif; font-size:9px; fill:#ffd700; font-weight:bold; }
      .fk-icon { font-family:'Segoe UI',Arial,sans-serif; font-size:9px; fill:#64b5f6; font-weight:bold; }
      .relation-line { stroke-width:2; fill:none; }
      .cardinality-label { font-family:'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:bold; }
      .draggable { cursor:move; }
    </style>
    <marker id="arrow-end" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L12,6 L0,12 L3,6 Z" fill="#64b5f6"/></marker>
    <marker id="one-circle" markerWidth="16" markerHeight="16" refX="8" refY="8" orient="auto" markerUnits="userSpaceOnUse"><circle cx="8" cy="8" r="5" fill="none" stroke="#64b5f6" stroke-width="2"/></marker>
    <marker id="many-crow" markerWidth="20" markerHeight="20" refX="18" refY="10" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,10 L18,2 M0,10 L18,10 M0,10 L18,18" stroke="#64b5f6" stroke-width="2" fill="none" stroke-linecap="round"/></marker>
    <marker id="one-line" markerWidth="16" markerHeight="20" refX="14" refY="10" orient="auto" markerUnits="userSpaceOnUse"><line x1="4" y1="2" x2="4" y2="18" stroke="#64b5f6" stroke-width="2.5" stroke-linecap="round"/><line x1="12" y1="2" x2="12" y2="18" stroke="#64b5f6" stroke-width="2.5" stroke-linecap="round"/></marker>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="2" dy="2" stdDeviation="3" flood-color="#000" flood-opacity="0.3"/></filter>
    <pattern id="gridpat" width="20" height="20" patternUnits="userSpaceOnUse"><path d="M 20 0 L 0 0 0 20" fill="none" stroke="#ffffff" stroke-opacity="0.07" stroke-width="1"/></pattern>
  </defs>'''

def marker(card, end):
    mk = "many-crow" if card == "N" else "one-line"
    return f'url(#{mk})'

def build_static_svg(L, order, tables, fkset, title):
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {L["cw"]} {L["ch"]}" width="{L["cw"]}" height="{L["ch"]}">']
    parts.append(DEFS)
    parts.append(f'  <rect width="100%" height="100%" fill="#1e1e1e"/>')
    parts.append('  <g class="relationships-layer">')
    for e in L["E"]:
        r = route(e, L["x"], L["y"], L["W"], L["H"])
        parts.append(f'    <g class="relationship" data-from-table="{esc(e["f"])}" data-to-table="{esc(e["t"])}">')
        parts.append(f'      <path d="{r["d"]}" class="relation-line" stroke="#64b5f6" stroke-opacity="0.6" stroke-width="2" marker-start="{marker(e["fc"],0)}" marker-end="{marker(e["tc"],1)}"/>')
        parts.append(f'      <rect x="{r["fbx"]:.0f}" y="{r["fby"]:.0f}" width="24" height="20" rx="4" fill="#2a2a2a" stroke="#64b5f6" stroke-width="1" opacity="0.9"/>')
        parts.append(f'      <text x="{r["ftx"]:.0f}" y="{r["fty"]:.0f}" text-anchor="middle" class="cardinality-label" fill="#64b5f6">{e["fc"]}</text>')
        parts.append(f'      <rect x="{r["tbx"]:.0f}" y="{r["tby"]:.0f}" width="24" height="20" rx="4" fill="#2a2a2a" stroke="#64b5f6" stroke-width="1" opacity="0.9"/>')
        parts.append(f'      <text x="{r["ttx"]:.0f}" y="{r["tty"]:.0f}" text-anchor="middle" class="cardinality-label" fill="#64b5f6">{e["tc"]}</text>')
        parts.append('    </g>')
    parts.append('  </g>')
    parts.append('  <g class="tables-layer">')
    for n in order:
        parts.append(table_markup(n, tables[n], L["x"][n], L["y"][n], fkset))
    parts.append('  </g>')
    parts.append('</svg>')
    return "\n".join(parts)

# ---- interactive HTML viewer ----
HTML_TPL = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root{ --bg:#1e1e1e; --bar:#252526; --line:#3a3a3a; --accent:#64b5f6; --hot:#ffd54f; }
  *{ box-sizing:border-box; }
  html,body{ margin:0; height:100%; background:var(--bg); color:#ddd; font-family:'Segoe UI',Arial,sans-serif; overflow:hidden; }
  #bar{ position:fixed; top:0; left:0; right:0; height:46px; display:flex; gap:8px; align-items:center; padding:0 12px; background:var(--bar); border-bottom:1px solid var(--line); z-index:10; font-size:13px; }
  #bar input[type=search]{ background:#1b1b1b; border:1px solid var(--line); color:#ddd; border-radius:6px; padding:5px 9px; width:230px; outline:none; }
  #bar button{ background:#333; border:1px solid var(--line); color:#ddd; border-radius:6px; padding:5px 10px; cursor:pointer; }
  #bar button:hover{ background:#3d3d3d; }
  #bar label{ display:flex; align-items:center; gap:5px; cursor:pointer; user-select:none; }
  #bar select{ background:#1b1b1b; border:1px solid var(--line); color:#ddd; border-radius:6px; padding:4px 6px; outline:none; }
  #bar .sep{ flex:1; }
  #hint{ color:#888; font-size:12px; }
  #stage{ position:absolute; top:46px; left:0; right:280px; bottom:0; overflow:hidden; touch-action:none; cursor:grab; }
  #stage.panning{ cursor:grabbing; }
  #side{ position:absolute; top:46px; right:0; bottom:0; width:280px; background:var(--bar); border-left:1px solid var(--line); overflow:auto; padding:12px; font-size:13px; }
  #side h3{ margin:0 0 6px; font-size:14px; color:#fff; word-break:break-all; }
  #side .meta{ color:#888; font-size:12px; margin-bottom:10px; }
  #side .grp{ color:var(--hot); font-size:11px; text-transform:uppercase; letter-spacing:.5px; margin:12px 0 4px; }
  #side a{ display:block; padding:4px 7px; border-radius:5px; color:#cfe3ff; text-decoration:none; cursor:pointer; }
  #side a:hover{ background:#333; }
  #side a small{ color:#888; }
  svg#svg{ display:block; width:100%; height:100%; }
  g.table{ cursor:pointer; }
  g.relationship{ transition:opacity .12s; }
  g.relationship.dim{ opacity:.045; }
  g.relationship.hot .relation-line{ stroke:var(--hot)!important; stroke-opacity:1!important; stroke-width:3.5!important; }
  g.table{ transition:opacity .12s; }
  g.table.dim{ opacity:.16; }
  g.table.sel .table-bg{ stroke:var(--hot)!important; stroke-width:3.5!important; }
  body.edit g.table{ cursor:move; }
  body.edit g.table .column{ pointer-events:none; }
</style>
</head>
<body>
<div id="bar">
  <strong style="color:#fff">__TITLE__</strong>
  <input id="find" type="search" placeholder="find table…" autocomplete="off">
  <button id="zin">+</button><button id="zout">&minus;</button><button id="fit">Fit</button>
  <span id="zlabel" style="color:#888; min-width:42px; text-align:center;">100%</span>
  <button id="reset">Clear</button>
  <label><input type="checkbox" id="edit"> Edit</label>
  <label title="Snap tables to a grid while dragging">Snap
    <select id="snap"><option value="0">off</option><option value="10">10</option><option value="20" selected>20</option><option value="26">26</option><option value="50">50</option></select>
  </label>
  <button id="save">Export SVG</button>
  <span class="sep"></span>
  <span id="hint">wheel: zoom · drag: pan · click a table to highlight</span>
</div>
<div id="stage">
  <svg id="svg" xmlns="http://www.w3.org/2000/svg">
__DEFS__
    <rect id="bg" width="100%" height="100%" fill="#1e1e1e"/>
    <g id="viewport">
      <rect id="gridrect" x="0" y="0" width="__VW__" height="__VH__" fill="url(#gridpat)" style="display:none" pointer-events="none"/>
      <g class="relationships-layer"></g>
      <g class="tables-layer">
__TABLES__
      </g>
    </g>
  </svg>
</div>
<div id="side">
  <h3 id="s-title">Select a table</h3>
  <div class="meta" id="s-meta">Hover or click a table to see its relationships.</div>
  <div id="s-list"></div>
</div>
<script>
const DATA = __DATA__;
const SVGNS="http://www.w3.org/2000/svg";
const svg=document.getElementById('svg');
const viewport=document.getElementById('viewport');
const relsLayer=svg.querySelector('.relationships-layer');
const stage=document.getElementById('stage');
const VW=__VW__, VH=__VH__;
const T={}; DATA.tables.forEach(t=>T[t.name]=Object.assign({},t));
document.querySelectorAll('g.table').forEach(el=>{const n=el.getAttribute('data-table'); if(T[n]) T[n].el=el;});
const ADJ={}; DATA.tables.forEach(t=>ADJ[t.name]=new Set());
DATA.rels.forEach(r=>{ if(r.f!==r.t){ADJ[r.f].add(r.t); ADJ[r.t].add(r.f);}});
function mk(card){ return card==='N' ? 'url(#many-crow)' : 'url(#one-line)'; }
function route(r){
  const f=T[r.f],t=T[r.t]; const fcx=f.x+f.w/2,tcx=t.x+t.w/2;
  const fay=f.y+r.foff, tay=t.y+r.toff; let p0,p3,fr,el2;
  if(tcx>=fcx){p0=[f.x+f.w,fay];p3=[t.x,tay];fr=true;el2=true;}
  else{p0=[f.x,fay];p3=[t.x+t.w,tay];fr=false;el2=false;}
  const co=Math.min(Math.max(Math.abs(p3[0]-p0[0])*0.5,45),160);
  const c1=[fr?p0[0]+co:p0[0]-co,p0[1]], c2=[el2?p3[0]-co:p3[0]+co,p3[1]];
  return {d:`M ${p0[0].toFixed(0)} ${p0[1].toFixed(0)} C ${c1[0].toFixed(0)} ${c1[1].toFixed(0)}, ${c2[0].toFixed(0)} ${c2[1].toFixed(0)}, ${p3[0].toFixed(0)} ${p3[1].toFixed(0)}`,
    fbx:(fr?p0[0]+16:p0[0]-40),fby:p0[1]-24,ftx:(fr?p0[0]+28:p0[0]-28),fty:p0[1]-10,
    tbx:(el2?p3[0]-40:p3[0]+16),tby:p3[1]-24,ttx:(el2?p3[0]-28:p3[0]+28),tty:p3[1]-10};
}
function el(tag,a){const e=document.createElementNS(SVGNS,tag);for(const k in a)e.setAttribute(k,a[k]);return e;}
DATA.rels.forEach(r=>{
  const g=el('g',{class:'relationship','data-from-table':r.f,'data-to-table':r.t});
  const vis=el('path',{class:'relation-line',stroke:'#64b5f6','stroke-opacity':'0.6','stroke-width':'2',fill:'none','marker-start':mk(r.fc),'marker-end':mk(r.tc)});
  const hit=el('path',{stroke:'transparent','stroke-width':'18',fill:'none'});
  const fb=el('rect',{width:24,height:20,rx:4,fill:'#2a2a2a',stroke:'#64b5f6','stroke-width':1,opacity:.9});
  const ft=el('text',{'text-anchor':'middle',class:'cardinality-label',fill:'#64b5f6'}); ft.textContent=r.fc;
  const tb=el('rect',{width:24,height:20,rx:4,fill:'#2a2a2a',stroke:'#64b5f6','stroke-width':1,opacity:.9});
  const tt=el('text',{'text-anchor':'middle',class:'cardinality-label',fill:'#64b5f6'}); tt.textContent=r.tc;
  g.append(vis,hit,fb,ft,tb,tt); relsLayer.append(g); r._g={g,vis,hit,fb,ft,tb,tt};
  g.addEventListener('mouseenter',()=>{if(!lock)hot(new Set([r.f,r.t]),x=>x===r);});
  g.addEventListener('mouseleave',()=>{if(!lock)clear();});
});
function drawRel(r){const g=r._g,m=route(r); g.vis.setAttribute('d',m.d); g.hit.setAttribute('d',m.d);
  g.fb.setAttribute('x',m.fbx);g.fb.setAttribute('y',m.fby);g.ft.setAttribute('x',m.ftx);g.ft.setAttribute('y',m.fty);
  g.tb.setAttribute('x',m.tbx);g.tb.setAttribute('y',m.tby);g.tt.setAttribute('x',m.ttx);g.tt.setAttribute('y',m.tty);}
DATA.rels.forEach(drawRel);
let lock=null, gesture=null, suppressClick=false;
function hot(nb,pred){
  DATA.rels.forEach(r=>{const on=pred(r); r._g.g.classList.toggle('hot',on); r._g.g.classList.toggle('dim',!on); if(on)relsLayer.append(r._g.g);});
  for(const n in T){const e=T[n].el; if(!e)continue; const keep=nb.has(n); e.classList.toggle('dim',!keep); e.classList.toggle('sel',lock===n);}
}
function highlight(name){const nb=new Set([name]); ADJ[name].forEach(k=>nb.add(k)); hot(nb,r=>r.f===name||r.t===name); panel(name);}
function clear(){DATA.rels.forEach(r=>r._g.g.classList.remove('hot','dim')); for(const n in T){T[n].el&&T[n].el.classList.remove('dim','sel');} if(!lock)resetPanel();}
document.querySelectorAll('g.table').forEach(e=>{const n=e.getAttribute('data-table');
  e.addEventListener('mouseenter',()=>{if(!lock && !gesture)highlight(n);});
  e.addEventListener('mouseleave',()=>{if(!lock && !gesture)clear();});
  e.addEventListener('click',ev=>{if(suppressClick)return; ev.stopPropagation(); if(lock===n){lock=null;clear();}else{lock=n;highlight(n);}});});
// (empty-canvas click clears the selection — handled in the pointerup handler below)
const sTitle=document.getElementById('s-title'),sMeta=document.getElementById('s-meta'),sList=document.getElementById('s-list');
function resetPanel(){sTitle.textContent='Select a table'; sMeta.textContent='Hover or click a table to see its relationships.'; sList.innerHTML='';}
function panel(name){
  const out=[],inc=[];
  DATA.rels.forEach(r=>{ if(r.f===name)out.push(r); else if(r.t===name)inc.push(r); });
  sTitle.textContent=name; sMeta.textContent=`${ADJ[name].size} linked tables · ${out.length+inc.length} relationships`;
  let h=''; const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const block=(title,arr,fn)=>{ if(!arr.length)return; h+=`<div class="grp">${title}</div>`;
    arr.slice().sort((a,b)=>fn(a).g.localeCompare(fn(b).g)).forEach(r=>{const o=fn(r);
      h+=`<a data-go="${esc(o.g)}">${esc(o.g)} <small>${o.card}</small><br><small>${esc(o.via)}</small></a>`;});};
  block('References (N→1)', out, r=>({g:r.t,card:`${r.fc}→${r.tc}`,via:`${r.fcol} → ${r.t}.${r.tcol}`}));
  block('Referenced by (1→N)', inc, r=>({g:r.f,card:`${r.fc}→${r.tc}`,via:`${r.f}.${r.fcol} → ${r.tcol}`}));
  sList.innerHTML=h;
  sList.querySelectorAll('a[data-go]').forEach(a=>a.addEventListener('click',ev=>{ev.stopPropagation(); const g=a.getAttribute('data-go'); lock=g; highlight(g); focusTable(g);}));
}
// view: screen = translate(tx,ty) * scale(k) * content
let k=1, tx=0, ty=0;
function applyView(){ viewport.setAttribute('transform','translate('+tx.toFixed(2)+' '+ty.toFixed(2)+') scale('+k+')');
  const z=document.getElementById('zlabel'); if(z) z.textContent=Math.round(k*100)+'%'; }
function fit(){ const sw=stage.clientWidth||1, sh=stage.clientHeight||1; k=(Math.min(sw/VW,sh/VH)*0.95)||1; tx=(sw-VW*k)/2; ty=(sh-VH*k)/2; applyView(); }
function zoomAt(factor,cxClient,cyClient){ const r=stage.getBoundingClientRect(); const mx=cxClient-r.left,my=cyClient-r.top;
  const ux=(mx-tx)/k, uy=(my-ty)/k; k=Math.min(Math.max(k*factor,0.02),8); tx=mx-ux*k; ty=my-uy*k; applyView(); }
function focusTable(n){ const t=T[n], sw=stage.clientWidth, sh=stage.clientHeight; tx=sw/2-(t.x+t.w/2)*k; ty=sh/2-(t.y+t.h/2)*k; applyView(); }
stage.addEventListener('wheel',function(e){ e.preventDefault(); e.stopPropagation(); zoomAt(e.deltaY<0?1.12:1/1.12,e.clientX,e.clientY); },{passive:false});
let editMode=false;
const snapSel=document.getElementById('snap');
const gridStep=()=>parseInt(snapSel.value,10)||0;
const snapVal=v=>{ const g=gridStep(); return g?Math.round(v/g)*g:v; };
function updateGrid(){ const g=gridStep(), r=document.getElementById('gridrect'), p=document.getElementById('gridpat');
  if(!r||!p) return;
  if(g){ p.setAttribute('width',g); p.setAttribute('height',g); p.firstElementChild.setAttribute('d','M '+g+' 0 L 0 0 0 '+g); }
  r.style.display=(editMode&&g)?'':'none'; }
snapSel.addEventListener('change',updateGrid);
document.getElementById('edit').addEventListener('change',e=>{editMode=e.target.checked; document.body.classList.toggle('edit',editMode); updateGrid();});
function onGestureDown(e){
  if(e.button!==0 && e.button!==1) return;
  const tableEl=e.target.closest && e.target.closest('g.table');
  if(editMode && e.button===0 && tableEl){ const n=tableEl.getAttribute('data-table');
    gesture={type:'drag', n, sx:e.clientX, sy:e.clientY, x0:T[n].x, y0:T[n].y, moved:false}; }
  else if(e.button===1 || (e.button===0 && !tableEl)){
    gesture={type:'pan', sx:e.clientX, sy:e.clientY, tx0:tx, ty0:ty, moved:false, btn:e.button}; }
  else return;
  e.preventDefault();
  window.addEventListener('pointermove', onGestureMove);
  window.addEventListener('pointerup', onGestureUp);
}
function onGestureMove(e){
  if(!gesture) return;
  const dx=e.clientX-gesture.sx, dy=e.clientY-gesture.sy;
  if(!gesture.moved && Math.abs(dx)+Math.abs(dy)>3){ gesture.moved=true; if(gesture.type==='pan') stage.classList.add('panning'); }
  if(gesture.type==='drag'){ const t=T[gesture.n]; t.x=snapVal(gesture.x0+dx/k); t.y=snapVal(gesture.y0+dy/k);
    t.el.setAttribute('transform','translate('+t.x.toFixed(1)+' '+t.y.toFixed(1)+')');
    DATA.rels.forEach(r=>{if(r.f===gesture.n||r.t===gesture.n)drawRel(r);}); }
  else { tx=gesture.tx0+dx; ty=gesture.ty0+dy; applyView(); }
}
function onGestureUp(){
  window.removeEventListener('pointermove', onGestureMove);
  window.removeEventListener('pointerup', onGestureUp);
  const gz=gesture; gesture=null; stage.classList.remove('panning');
  if(gz && gz.moved){ suppressClick=true; setTimeout(()=>{suppressClick=false;},0); }
  else if(gz && gz.type==='pan' && gz.btn===0 && lock){ lock=null; clear(); }
}
stage.addEventListener('pointerdown', onGestureDown);
stage.addEventListener('mousedown',e=>{if(e.button===1)e.preventDefault();});
stage.addEventListener('auxclick',e=>{if(e.button===1)e.preventDefault();});
function stageCenter(){ const r=stage.getBoundingClientRect(); return [r.left+r.width/2,r.top+r.height/2]; }
document.getElementById('zin').onclick=()=>{const c=stageCenter(); zoomAt(1.25,c[0],c[1]);};
document.getElementById('zout').onclick=()=>{const c=stageCenter(); zoomAt(1/1.25,c[0],c[1]);};
document.getElementById('fit').onclick=fit;
document.getElementById('reset').onclick=()=>{lock=null;clear();};
document.getElementById('find').addEventListener('input',e=>{const q=e.target.value.trim().toLowerCase(); if(!q)return;
  const hit=Object.keys(T).find(n=>n.toLowerCase().includes(q)); if(hit){lock=hit;highlight(hit);focusTable(hit);}});
document.getElementById('save').onclick=()=>{lock=null;clear();
  const clone=svg.cloneNode(true); clone.setAttribute('viewBox',`0 0 ${VW} ${VH}`); clone.setAttribute('width',VW); clone.setAttribute('height',VH);
  const vp=clone.querySelector('#viewport'); if(vp) vp.removeAttribute('transform');
  const gr=clone.querySelector('#gridrect'); if(gr) gr.remove();
  const s='<?xml version="1.0" encoding="UTF-8"?>\n'+new XMLSerializer().serializeToString(clone);
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([s],{type:'image/svg+xml'})); a.download='__TITLE___untangled.svg'; a.click();};
fit();
</script>
</body>
</html>'''

def build_html(L, order, tables, fkset, title):
    tabs = "\n".join(table_markup(n, tables[n], L["x"][n], L["y"][n], fkset, pad="      ") for n in order)
    data = {
        "tables": [{"name": n, "x": round(L["x"][n]), "y": round(L["y"][n]),
                    "w": L["W"][n], "h": L["H"][n]} for n in order],
        "rels": [{"f": e["f"], "t": e["t"], "foff": round(e["foff"], 1), "toff": round(e["toff"], 1),
                  "fc": e["fc"], "tc": e["tc"], "fcol": e["fcol"], "tcol": e["tcol"]} for e in L["E"]],
    }
    return (HTML_TPL.replace("__DEFS__", DEFS)
                    .replace("__TABLES__", tabs)
                    .replace("__DATA__", json.dumps(data, ensure_ascii=False))
                    .replace("__VW__", str(L["cw"]))
                    .replace("__VH__", str(L["ch"]))
                    .replace("__TITLE__", esc(title)))

# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Untangle a DBML schema into an interactive ER diagram.")
    ap.add_argument("dbml", nargs="+",
                    help="input .dbml file(s); with several files they are merged "
                         "and a table redefined in a later file wins")
    ap.add_argument("-o", "--out", help="output basename (default: input name)")
    ap.add_argument("--seeds", type=int, default=10, help="layout attempts (more = better, slower)")
    ap.add_argument("--no-svg", action="store_true", help="skip the static .svg output")
    ap.add_argument("--no-html", action="store_true", help="skip the interactive .html output")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    order, tables, refs = merge_files(args.dbml, quiet=args.quiet)
    if not args.quiet:
        print(f"parsed {len(tables)} tables, {len(refs)} relationships")
    if not tables:
        print("no tables found - is this a DBML file?", file=sys.stderr); sys.exit(1)

    L = layout(order, tables, refs, seeds=args.seeds, verbose=not args.quiet)
    if not args.quiet:
        print(f"layout: crossings={L['crossings']} length={L['length']:.0f} canvas={L['cw']}x{L['ch']}")

    base = args.out or os.path.splitext(args.dbml[0])[0]
    title = os.path.basename(base)
    fkset = fk_columns(refs)

    if not args.no_svg:
        open(base + ".svg", "w", encoding="utf-8").write(build_static_svg(L, order, tables, fkset, title))
        if not args.quiet: print("wrote", base + ".svg")
    if not args.no_html:
        open(base + ".html", "w", encoding="utf-8").write(build_html(L, order, tables, fkset, title))
        if not args.quiet: print("wrote", base + ".html")

if __name__ == "__main__":
    main()
