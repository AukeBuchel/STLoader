#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEGO STL Downloader
===================
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import os
import queue
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

APP_NAME = "LEGO STL Downloader"
APP_VERSION = "1.0"

OFFICIAL_URL = os.environ.get(
    "LEGO_STL_OFFICIAL_URL", "https://library.ldraw.org/library/updates/complete.zip")
UNOFFICIAL_URL = os.environ.get(
    "LEGO_STL_UNOFFICIAL_URL", "https://library.ldraw.org/library/unofficial/ldrawunf.zip")
USER_AGENT = "LEGO-STL-Downloader/%s (personal 3D-printing tool; Python urllib)" % APP_VERSION

LDU_MM = 0.4            # 1 LDraw unit = 0.4 mm  ->  STLs are true LEGO size
REFRESH_DAYS = 90       # re-download the library when the cached copy is older


class ToolError(Exception):
    """An error with a message meant for the user."""


# ---------------------------------------------------------------------------
# Settings and cache folder
# ---------------------------------------------------------------------------

def app_dir() -> str:
    base = os.environ.get("LEGO_STL_CACHE_DIR")
    if not base:
        if os.name == "nt":
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
        else:
            base = os.path.join(os.path.expanduser("~"), ".cache", "lego-stl-downloader")
    os.makedirs(base, exist_ok=True)
    return base


def settings_path() -> str:
    return os.path.join(app_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reading the parts list
# ---------------------------------------------------------------------------

# Header names (lower case, letters/digits only) per role, best match first.
ROLE_HEADERS = {
    "part": ["ldrawid", "ldrawpart", "ldrawpartid", "ldraw", "designid", "design", "designno",
             "partnum", "partnumber", "partno", "partid", "part", "blitemno", "itemno",
             "itemnumber", "itemid", "item", "boid"],
    "qty": ["qty", "quantity", "count", "amount", "minqty", "qtyneeded", "pieces", "number"],
    "color": ["colorname", "colourname", "colour", "color", "blcolorname", "ldrawcolorid",
              "ldrawcolourid", "colorid", "colourid", "blcolorid"],
    "name": ["elementname", "partname", "name", "description", "itemname", "desc"],
    "spare": ["isspare", "spare", "spares"],
}


def _norm_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was decoded as Windows-1252 (e.g. 'Â°' -> '°')."""
    if not text or not any(ch in text for ch in "ÃÂâ"):
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


class PartRow:
    __slots__ = ("part_id", "name", "color", "qty", "spare")

    def __init__(self, part_id, name, color, qty, spare):
        self.part_id = part_id
        self.name = name
        self.color = color
        self.qty = qty
        self.spare = spare


class PartsList:
    def __init__(self, path, rows, columns, warnings):
        self.path = path
        self.rows = rows            # list[PartRow]
        self.columns = columns      # role -> header text
        self.warnings = warnings    # list[str]

    def selected(self, include_spares: bool):
        return [r for r in self.rows if include_spares or not r.spare]

    def summary(self, include_spares: bool) -> dict:
        rows = self.selected(include_spares)
        return {
            "lines": len(rows),
            "parts": len({r.part_id.lower() for r in rows}),
            "pieces": sum(r.qty for r in rows),
            "colours": len({r.color for r in rows if r.color}),
            "spare_lines": sum(1 for r in self.rows if r.spare),
        }


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_parts_csv(path: str) -> PartsList:
    text = _read_text(path)
    lines = [ln for ln in text.splitlines() if ln.strip().strip(",;\t")]
    if not lines:
        raise ToolError("The file is empty.")
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    table = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    header = [h.strip() for h in table[0]]
    normalized = [_norm_header(h) for h in header]

    columns = {}
    for role, candidates in ROLE_HEADERS.items():
        for cand in candidates:
            if cand in normalized:
                columns[role] = normalized.index(cand)
                break
    if "part" not in columns:
        raise ToolError(
            "Couldn't find a part number column.\n\nColumns in this file: %s\n\n"
            "Expected a column named e.g. DesignID, Part, LdrawId or ItemID." % ", ".join(header))

    rows, warnings = [], []
    for line_no, cells in enumerate(table[1:], start=2):
        def cell(role):
            i = columns.get(role)
            return cells[i].strip() if i is not None and i < len(cells) else ""
        part_id = cell("part")
        if not part_id:
            continue
        qty_text = cell("qty")
        if "qty" in columns:
            try:
                qty = int(round(float(qty_text.replace(",", "."))))
            except ValueError:
                warnings.append("Line %d: quantity '%s' isn't a number, using 1." % (line_no, qty_text))
                qty = 1
        else:
            qty = 1
        if qty <= 0:
            continue
        spare = cell("spare").lower() in ("true", "yes", "y", "t", "1")
        rows.append(PartRow(part_id, fix_mojibake(cell("name")), fix_mojibake(cell("color")), qty, spare))
    if not rows:
        raise ToolError("No parts found in this file.")
    names = {role: header[i] for role, i in columns.items()}
    return PartsList(path, rows, names, warnings)


# ---------------------------------------------------------------------------
# LDraw library: indexing, part lookup and geometry
# ---------------------------------------------------------------------------

def clean_title(title: str) -> str:
    title = title.lstrip("~=_ ").replace("(Obsolete)", "")
    return re.sub(r"\s+", " ", title).strip()


def _det3(a, b, c, d, e, f, g, h, i):
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _append_transformed(out, src, m, flip):
    """Append triangles from src (flat xyz list) transformed by m to out."""
    x0, y0, z0, a, b, c, d, e, f, g, h, i = m
    ext = out.extend
    for k in range(0, len(src), 9):
        p0, p1, p2, p3, p4, p5, p6, p7, p8 = src[k:k + 9]
        ax = a * p0 + b * p1 + c * p2 + x0
        ay = d * p0 + e * p1 + f * p2 + y0
        az = g * p0 + h * p1 + i * p2 + z0
        bx = a * p3 + b * p4 + c * p5 + x0
        by = d * p3 + e * p4 + f * p5 + y0
        bz = g * p3 + h * p4 + i * p5 + z0
        cx = a * p6 + b * p7 + c * p8 + x0
        cy = d * p6 + e * p7 + f * p8 + y0
        cz = g * p6 + h * p7 + i * p8 + z0
        if flip:
            ext((ax, ay, az, cx, cy, cz, bx, by, bz))
        else:
            ext((ax, ay, az, bx, by, bz, cx, cy, cz))


class Resolution:
    def __init__(self, key, source, note=""):
        self.key = key          # e.g. "parts/3794b.dat"
        self.source = source    # "official" or "unofficial"
        self.note = note

    @property
    def file_name(self):
        return self.key.split("/")[-1]


class LDrawLibrary:
    def __init__(self, hires: bool = True):
        self.hires = hires
        self.zips = []          # [(label, ZipFile)]
        self.index = {}         # key -> [(zip_no, member)]
        self.colours = {}       # LDraw colour code -> name
        self._text = {}
        self._parsed = {}
        self._geom = {}
        self.missing = set()

    # -- loading ---------------------------------------------------------
    def add_zip(self, path: str, label: str) -> int:
        try:
            zf = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ToolError("Couldn't open the LDraw library zip:\n%s\n\n%s" % (path, exc))
        zip_no = len(self.zips)
        self.zips.append((label, zf))
        count = 0
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            low = name.lower()
            if low.endswith("ldconfig.ldr") and not self.colours:
                self._read_colours(zf.read(info.filename))
                continue
            if not low.endswith(".dat"):
                continue
            comps = low.split("/")
            for pos, comp in enumerate(comps[:-1]):
                if comp in ("parts", "p"):
                    key = "/".join(comps[pos:])
                    self.index.setdefault(key, []).append((zip_no, info.filename))
                    count += 1
                    break
        if count == 0:
            raise ToolError("This zip doesn't contain an LDraw parts library:\n%s" % path)
        return count

    def _read_colours(self, data: bytes):
        for line in data.decode("utf-8", "replace").splitlines():
            toks = line.split()
            if len(toks) >= 5 and toks[1] == "!COLOUR" and "CODE" in toks:
                try:
                    code = toks[toks.index("CODE") + 1]
                    self.colours[code] = toks[2].replace("_", " ")
                except IndexError:
                    pass

    # -- raw file access -------------------------------------------------
    def _member(self, key, prefer):
        entries = self.index.get(key)
        if not entries:
            return None
        for zip_no, member in entries:
            if self.zips[zip_no][0] == prefer:
                return zip_no, member
        return entries[0]

    def has(self, key) -> bool:
        return key in self.index

    def source_of(self, key, prefer="official") -> str:
        hit = self._member(key, prefer)
        if not hit:
            return ""
        label = self.zips[hit[0]][0]
        if label == "official" and "unofficial" in self.org(key, prefer).lower():
            return "unofficial"
        return label

    def text(self, key, prefer="official") -> str:
        hit = self._member(key, prefer)
        if hit is None:
            return ""
        if hit not in self._text:
            data = self.zips[hit[0]][1].read(hit[1])
            self._text[hit] = data.decode("utf-8", "replace")
        return self._text[hit]

    def title(self, key, prefer="official") -> str:
        for line in self.text(key, prefer).splitlines():
            line = line.strip()
            if line:
                return line[1:].strip() if line.startswith("0") else ""
        return ""

    def org(self, key, prefer="official") -> str:
        for line in self.text(key, prefer).splitlines()[:30]:
            if "!LDRAW_ORG" in line:
                return line.split("!LDRAW_ORG", 1)[1].strip()
        return ""

    def lookup(self, ref: str):
        """Find the library key for a sub-file reference such as 's\\3001s01.dat'."""
        name = ref.strip().lower().replace("\\", "/")
        while name.startswith("./"):
            name = name[2:]
        candidates = []
        if self.hires and "/" not in name:
            candidates.append("p/48/" + name)
        candidates += ["p/" + name, "parts/" + name]
        for cand in candidates:
            if cand in self.index:
                return cand
        return None

    # -- resolving part numbers ------------------------------------------
    def _variants(self, base):
        found = []
        for letter in "abcdefghijklmnopqrstuvwxyz":
            key = "parts/%s%s.dat" % (base, letter)
            if key in self.index and not self.title(key).startswith("~"):
                found.append(key)
        official = [k for k in found if self.source_of(k) == "official"]
        return official or found

    def _resolve_one(self, pid, depth=0):
        key = "parts/%s.dat" % pid
        if key not in self.index:
            variants = self._variants(pid)
            if variants:
                names = ", ".join(v.split("/")[-1][:-4] for v in variants)
                return Resolution(variants[-1], self.source_of(variants[-1]),
                                  "no plain %s in LDraw; used the newest version (%s)" % (pid, names))
            return None
        title = self.title(key)
        if title.startswith("~"):
            variants = self._variants(pid)
            if variants:
                chosen = variants[-1]
                names = ", ".join(v.split("/")[-1][:-4] for v in variants)
                note = "newest version of %s" % pid if len(variants) == 1 else \
                    "newest of %d versions (%s)" % (len(variants), names)
                return Resolution(chosen, self.source_of(chosen), note)
            moved = re.match(r"~moved to\s+(\S+)", title, re.I)
            if moved and depth < 5:
                target = moved.group(1).lower()
                if target.endswith(".dat"):
                    target = target[:-4]
                res = self._resolve_one(target, depth + 1)
                if res:
                    res.note = ("moved to %s; " % target + res.note).strip("; ")
                    return res
            return Resolution(key, self.source_of(key), "obsolete LDraw part")
        note = ""
        if title.startswith("="):
            ref = re.search(r"^1\s.*?(\S+\.dat)\s*$", self.text(key), re.M | re.I)
            note = "alias of %s" % ref.group(1)[:-4] if ref else "alias"
        return Resolution(key, self.source_of(key), note)

    def resolve(self, part_id: str):
        pid = part_id.strip().lower().replace(" ", "")
        if pid.endswith(".dat"):
            pid = pid[:-4]
        if not pid:
            return None
        res = self._resolve_one(pid)
        if res:
            return res
        # Printed, patterned or stickered versions: fall back to the plain mould.
        base = re.sub(r"(pr|pb|pat|px|ps|pc|stk|p)[0-9a-z]*$", "", pid)
        if base and base != pid and re.search(r"\d", base):
            res = self._resolve_one(base)
            if res:
                res.note = ("printed/patterned part; used plain %s. " % base + res.note).strip()
                return res
        return None

    # -- geometry ----------------------------------------------------------
    def _parse(self, key, prefer):
        cache_key = (key, prefer)
        if cache_key in self._parsed:
            return self._parsed[cache_key]
        cmds = []
        certified = False
        winding_cw = False
        invert_next = False
        for raw in self.text(key, prefer).splitlines():
            line = raw.strip()
            if not line:
                continue
            kind = line[0]
            if kind == "0":
                toks = line.split()
                if len(toks) >= 2 and toks[1] == "BFC":
                    words = [t.upper() for t in toks[2:]]
                    if "NOCERTIFY" in words:
                        certified = False
                    elif "CERTIFY" in words:
                        certified = True
                    if "CW" in words:
                        winding_cw = True
                    elif "CCW" in words:
                        winding_cw = False
                    if "INVERTNEXT" in words:
                        invert_next = True
                continue
            try:
                if kind == "1":
                    toks = line.split(None, 14)
                    if len(toks) == 15:
                        cmds.append(("r", tuple(float(t) for t in toks[2:14]), toks[14].strip(), invert_next))
                    invert_next = False
                elif kind in "34":
                    n = 3 if kind == "3" else 4
                    toks = line.split()
                    v = [float(t) for t in toks[2:2 + 3 * n]]
                    if len(v) < 3 * n:
                        continue
                    tris = [v] if n == 3 else [v[0:9], v[0:3] + v[6:12]]
                    reverse = certified and winding_cw
                    for t in tris:
                        if reverse:
                            t = t[0:3] + t[6:9] + t[3:6]
                        cmds.append(("t", t))
                    invert_next = False
            except ValueError:
                continue
        self._parsed[cache_key] = cmds
        return cmds

    def geometry(self, key, prefer="official", _stack=()):
        """Triangles of a file as a flat list (9 floats each), LDraw units/axes."""
        cache_key = (key, prefer)
        if cache_key in self._geom:
            return self._geom[cache_key]
        if key in _stack:
            return []
        out = []
        for cmd in self._parse(key, prefer):
            if cmd[0] == "t":
                out.extend(cmd[1])
                continue
            _, m, ref, invert = cmd
            child_key = self.lookup(ref)
            if child_key is None:
                self.missing.add(ref)
                continue
            child = self.geometry(child_key, prefer, _stack + (key,))
            if child:
                flip = invert ^ (_det3(*m[3:]) < 0)
                _append_transformed(out, child, m, flip)
        self._geom[cache_key] = out
        return out

    def pieces(self, key, prefer="official"):
        """For a shortcut (an assembly of several parts) return its pieces."""
        if "shortcut" not in self.org(key, prefer).lower():
            return None
        cmds = self._parse(key, prefer)
        if any(c[0] == "t" for c in cmds):
            return None
        result = []
        for _, m, ref, _inv in cmds:
            child = self.lookup(ref)
            if child is None or not child.startswith("parts/") or child.startswith("parts/s/"):
                return None
            result.append(child)
        return result if len(result) > 1 else None

    def forget_part_geometry(self):
        """Free memory: keep only the cached primitives and sub-parts."""
        self._geom = {k: v for k, v in self._geom.items()
                      if k[0].startswith("p/") or k[0].startswith("parts/s/")}


# ---------------------------------------------------------------------------
# STL output
# ---------------------------------------------------------------------------

def to_print_mesh(tris):
    """LDraw units/axes -> millimetres, Z up, outward-facing, resting on z = 0."""
    s = LDU_MM
    out = [0.0] * len(tris)
    for k in range(0, len(tris), 3):
        x, y, z = tris[k], tris[k + 1], tris[k + 2]
        out[k] = x * s          # LDraw -Y is up; this mapping keeps handedness
        out[k + 1] = z * s
        out[k + 2] = -y * s
    if not out:
        return out
    volume = 0.0
    for k in range(0, len(out), 9):
        ax, ay, az, bx, by, bz, cx, cy, cz = out[k:k + 9]
        volume += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)
    if volume < 0:
        for k in range(0, len(out), 9):
            out[k + 3:k + 6], out[k + 6:k + 9] = out[k + 6:k + 9], out[k + 3:k + 6]
    xs, ys, zs = out[0::3], out[1::3], out[2::3]
    dx = (min(xs) + max(xs)) / 2.0
    dy = (min(ys) + max(ys)) / 2.0
    dz = min(zs)
    for k in range(0, len(out), 3):
        out[k] -= dx
        out[k + 1] -= dy
        out[k + 2] -= dz
    return out


def stl_bytes(tris, title: str) -> bytes:
    pack = struct.Struct("<12fH").pack
    body = []
    count = 0
    for k in range(0, len(tris), 9):
        ax, ay, az, bx, by, bz, cx, cy, cz = tris[k:k + 9]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length < 1e-12:
            continue                      # skip degenerate triangles
        body.append(pack(nx / length, ny / length, nz / length,
                         ax, ay, az, bx, by, bz, cx, cy, cz, 0))
        count += 1
    header = ("LDraw %s - %s" % (title, APP_NAME)).encode("ascii", "replace")[:80].ljust(80, b" ")
    return header + struct.pack("<I", count) + b"".join(body)


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, limit: int = 110) -> str:
    text = _BAD_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return (text[:limit].rstrip(". ") or "unnamed")


# ---------------------------------------------------------------------------
# Downloading the library
# ---------------------------------------------------------------------------

def download(url: str, dest: str, emit, what: str) -> None:
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > 0.2:
                    last = now
                    if total:
                        emit("progress", done / total)
                        emit("status", "Downloading %s: %.0f of %.0f MB" % (what, done / 1e6, total / 1e6))
                    else:
                        emit("status", "Downloading %s: %.0f MB" % (what, done / 1e6))
    except (urllib.error.URLError, OSError) as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ToolError("Couldn't download the %s from\n%s\n\n%s" % (what, url, exc))
    if not zipfile.is_zipfile(tmp):
        os.remove(tmp)
        raise ToolError("The %s download from\n%s\nwasn't a zip file (the site may be blocking "
                        "automatic downloads)." % (what, url))
    os.replace(tmp, dest)


MANUAL_HELP = (
    "\n\nYou can download it yourself instead: open\n%s\nin your browser, save the zip, then "
    "click 'Use a downloaded library zip…' and choose it." % OFFICIAL_URL)


def official_library_path(emit, settings) -> str:
    custom = settings.get("library_zip")
    if custom and os.path.isfile(custom):
        return custom
    path = os.path.join(app_dir(), "complete.zip")
    fresh = os.path.isfile(path) and (time.time() - os.path.getmtime(path)) < REFRESH_DAYS * 86400
    if fresh:
        return path
    try:
        emit("status", "Updating the LDraw parts library…" if os.path.isfile(path) else
             "Downloading the LDraw parts library (only needed once)…")
        download(OFFICIAL_URL, path, emit, "LDraw parts library")
    except ToolError as exc:
        if os.path.isfile(path):
            emit("status", "Couldn't update the parts library; using the copy from %s."
                 % _dt.date.fromtimestamp(os.path.getmtime(path)).isoformat())
            return path
        raise ToolError(str(exc) + MANUAL_HELP)
    return path


def unofficial_library_path(emit):
    path = os.path.join(app_dir(), "ldrawunf.zip")
    fresh = os.path.isfile(path) and (time.time() - os.path.getmtime(path)) < 30 * 86400
    if not fresh:
        try:
            emit("status", "Downloading unofficial LDraw parts…")
            download(UNOFFICIAL_URL, path, emit, "unofficial LDraw parts")
        except ToolError:
            if not os.path.isfile(path):
                return None
    return path


def library_status_text(settings) -> str:
    custom = settings.get("library_zip")
    if custom and os.path.isfile(custom):
        return "Parts library: your zip (%s)" % os.path.basename(custom)
    path = os.path.join(app_dir(), "complete.zip")
    if os.path.isfile(path):
        when = _dt.date.fromtimestamp(os.path.getmtime(path)).strftime("%d %b %Y")
        return "Parts library: LDraw, downloaded %s" % when
    return "Parts library: LDraw (downloaded automatically on the first run, about 100 MB)"


# ---------------------------------------------------------------------------
# The job: CSV rows -> STL files
# ---------------------------------------------------------------------------

def run_job(parts: PartsList, out_dir: str, by_colour: bool, include_spares: bool,
            use_unofficial: bool, emit, settings=None) -> dict:
    settings = settings if settings is not None else load_settings()
    rows = parts.selected(include_spares)
    by_colour = by_colour and "color" in parts.columns

    lib = LDrawLibrary()
    lib.add_zip(official_library_path(emit, settings), "official")

    # Group rows: one STL per part per colour folder, quantities summed.
    groups = {}     # part id (lower) -> {"id", "names", "folders": {folder: qty}}
    for r in rows:
        g = groups.setdefault(r.part_id.lower(), {"id": r.part_id, "names": [], "folders": {}})
        if r.name and r.name not in g["names"]:
            g["names"].append(r.name)
        folder = (r.color or "") if by_colour else ""
        if by_colour and r.color.isdigit():
            colour_name = lib.colours.get(r.color)
            folder = "%s %s" % (r.color, colour_name) if colour_name else "Colour %s" % r.color
        g["folders"][folder] = g["folders"].get(folder, 0) + r.qty

    emit("status", "Matching %d parts with the LDraw library…" % len(groups))
    resolved = {pid: lib.resolve(g["id"]) for pid, g in groups.items()}
    if use_unofficial and any(v is None for v in resolved.values()):
        path = unofficial_library_path(emit)
        if path:
            try:
                lib.add_zip(path, "unofficial")
                for pid, res in resolved.items():
                    if res is None:
                        resolved[pid] = lib.resolve(groups[pid]["id"])
            except ToolError:
                pass

    os.makedirs(out_dir, exist_ok=True)
    report = []
    written = 0
    failed = 0
    for n, (pid, g) in enumerate(sorted(groups.items()), start=1):
        emit("progress", (n - 1) / max(1, len(groups)))
        emit("status", "Converting %s (%d of %d)…" % (g["id"], n, len(groups)))
        res = resolved[pid]
        csv_name = " / ".join(g["names"])
        if res is None:
            failed += 1
            emit("part", pid, "✗ not in LDraw", False)
            for folder, qty in g["folders"].items():
                report.append([folder, g["id"], csv_name, qty, "", "", "", "not found",
                               "no LDraw model with this number", ""])
            continue
        try:
            lib.missing = set()
            prefer = res.source if res.source == "unofficial" else "official"
            pieces = lib.pieces(res.key, prefer) or [res.key]
            outputs = []   # (suffix, title, bytes)
            for i, key in enumerate(pieces, start=1):
                mesh = to_print_mesh(lib.geometry(key, prefer))
                if not mesh:
                    raise ToolError("no geometry")
                title = clean_title(lib.title(key, prefer))
                suffix = "" if len(pieces) == 1 else " - piece %d of %d - %s" % (i, len(pieces), title)
                outputs.append((suffix, title, stl_bytes(mesh, key.split("/")[-1][:-4])))
            main_title = clean_title(lib.title(res.key, prefer))
            note = res.note
            if len(pieces) > 1:
                note = ("assembly split into %d printable pieces. " % len(pieces) + note).strip()
            if lib.missing:
                note = (note + " Missing sub-files: %s." % ", ".join(sorted(lib.missing))).strip()
            for folder, qty in g["folders"].items():
                target = os.path.join(out_dir, safe_name(folder)) if folder else out_dir
                os.makedirs(target, exist_ok=True)
                files = []
                for suffix, title, data in outputs:
                    base = "%s - %s" % (g["id"], main_title) if not suffix else "%s%s" % (g["id"], suffix)
                    fname = safe_name(base) + " (x%d).stl" % qty
                    with open(os.path.join(target, fname), "wb") as f:
                        f.write(data)
                    files.append(os.path.relpath(os.path.join(target, fname), out_dir))
                    written += 1
                report.append([folder, g["id"], csv_name, qty, res.file_name, main_title, res.source,
                               "ok" if not lib.missing else "ok (incomplete)", note, " | ".join(files)])
            label = res.file_name[:-4]
            label = "✓ %s" % label if label.lower() == pid else "✓ %s → %s" % (g["id"], label)
            if len(pieces) > 1:
                label += " (%d pieces)" % len(pieces)
            if res.source == "unofficial":
                label += " (unofficial)"
            emit("part", pid, label, True)
        except Exception as exc:  # keep going with the other parts
            failed += 1
            emit("part", pid, "✗ conversion failed", False)
            for folder, qty in g["folders"].items():
                report.append([folder, g["id"], csv_name, qty, res.file_name, "", res.source,
                               "failed", str(exc), ""])
        lib.forget_part_geometry()

    report_path = os.path.join(out_dir, "parts_report.csv")
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Colour", "Part", "Name in parts list", "Quantity", "LDraw file", "LDraw name",
                    "Library", "Result", "Note", "STL file(s)"])
        w.writerows(sorted(report, key=lambda r: (r[0], r[1])))
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README_TEXT % {"csv": os.path.basename(parts.path),
                               "date": _dt.date.today().isoformat(),
                               "app": APP_NAME})
    summary = {"parts": len(groups), "files": written, "failed": failed, "out": out_dir}
    emit("progress", 1.0)
    emit("done", summary)
    return summary


README_TEXT = """STL files made by %(app)s from %(csv)s on %(date)s.

Part shapes: LDraw Parts Library, https://www.ldraw.org
Licensed under Creative Commons Attribution (CC BY 2.0 / CC BY 4.0); each
part's authors are listed in its LDraw file. LDraw is a trademark of the
Estate of James Jessiman. LEGO is a trademark of the LEGO Group, which does
not sponsor or endorse this.

Printing notes
- Scale: 1 LDraw unit = 0.4 mm, so the parts are true LEGO size (100%%).
- Studs are separate shells sitting on top of each part. Load each STL as one
  object and don't use "Split to objects", or the studs come loose.
- LDraw draws bricks without LEGO's 0.1 mm gap on each side
  (a 2x4 brick is 32.0 x 16.0 mm instead of 31.8 x 15.8 mm).
- Round features use LDraw's 48-segment high-resolution primitives.
- parts_report.csv lists every part, which LDraw model was used, and why.
"""


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import tkinter.font as tkfont

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.title(APP_NAME)
    root.minsize(780, 560)
    root.geometry("900x640")

    style = ttk.Style(root)
    if os.name != "nt" and "clam" in style.theme_names():
        style.theme_use("clam")
    bold = tkfont.nametofont("TkDefaultFont").copy()
    bold.configure(weight="bold")

    settings = load_settings()
    state = {"parts": None, "items": {}, "worker": None}
    q = queue.Queue()

    csv_var = tk.StringVar()
    out_var = tk.StringVar()
    summary_var = tk.StringVar(value="Choose a parts list (CSV) to begin.")
    status_var = tk.StringVar()
    library_var = tk.StringVar(value=library_status_text(settings))
    by_colour = tk.BooleanVar(value=True)
    spares = tk.BooleanVar(value=False)
    unofficial = tk.BooleanVar(value=True)

    main = ttk.Frame(root, padding=14)
    main.pack(fill="both", expand=True)
    main.columnconfigure(1, weight=1)
    main.rowconfigure(2, weight=1)

    ttk.Label(main, text="Parts list (CSV)").grid(row=0, column=0, sticky="w")
    ttk.Entry(main, textvariable=csv_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=8)
    ttk.Button(main, text="Choose CSV…", command=lambda: choose_csv()).grid(row=0, column=2, sticky="ew")

    ttk.Label(main, textvariable=summary_var, font=bold).grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(12, 6))

    table = ttk.Frame(main)
    table.grid(row=2, column=0, columnspan=3, sticky="nsew")
    cols = ("part", "name", "colour", "qty", "result")
    tree = ttk.Treeview(table, columns=cols, show="headings", selectmode="browse")
    for col, text, width, anchor in (("part", "Part", 80, "w"), ("name", "Name", 240, "w"),
                                     ("colour", "Colour", 175, "w"), ("qty", "Qty", 50, "e"),
                                     ("result", "Result", 230, "w")):
        tree.heading(col, text=text, anchor=anchor)
        tree.column(col, width=width, anchor=anchor, stretch=(col in ("name", "result")))
    tree.tag_configure("spare", foreground="#888888")
    tree.tag_configure("bad", foreground="#B00020")
    scroll = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    ttk.Label(main, text="Output folder").grid(row=3, column=0, sticky="w", pady=(14, 0))
    ttk.Entry(main, textvariable=out_var).grid(row=3, column=1, sticky="ew", padx=8, pady=(14, 0))
    ttk.Button(main, text="Choose folder…", command=lambda: choose_out()).grid(
        row=3, column=2, sticky="ew", pady=(14, 0))

    opts = ttk.Frame(main)
    opts.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
    colour_cb = ttk.Checkbutton(opts, text="Sort into a folder per colour", variable=by_colour)
    colour_cb.pack(side="left")
    spare_cb = ttk.Checkbutton(opts, text="Include spare parts", variable=spares,
                               command=lambda: refresh_table())
    spare_cb.pack(side="left", padx=(18, 0))
    ttk.Checkbutton(opts, text="Also search unofficial LDraw parts", variable=unofficial).pack(
        side="left", padx=(18, 0))

    lib_row = ttk.Frame(main)
    lib_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    ttk.Label(lib_row, textvariable=library_var, foreground="#555555").pack(side="left")
    lib_btn = ttk.Button(lib_row, text="Use a downloaded library zip…", command=lambda: choose_library())
    lib_btn.pack(side="right")

    run_row = ttk.Frame(main)
    run_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(16, 0))
    run_row.columnconfigure(1, weight=1)
    run_btn = ttk.Button(run_row, text="Download STLs", command=lambda: start())
    run_btn.grid(row=0, column=0)
    progress = ttk.Progressbar(run_row, mode="determinate", maximum=1.0)
    progress.grid(row=0, column=1, sticky="ew", padx=10)
    open_btn = ttk.Button(run_row, text="Open folder", state="disabled", command=lambda: open_out())
    open_btn.grid(row=0, column=2)
    ttk.Label(main, textvariable=status_var, wraplength=840, justify="left").grid(
        row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # -- actions --------------------------------------------------------------
    def refresh_table():
        parts = state["parts"]
        tree.delete(*tree.get_children())
        state["items"] = {}
        if parts is None:
            return
        for r in parts.rows:
            if r.spare and not spares.get():
                continue
            item = tree.insert("", "end", values=(r.part_id, r.name, r.color,
                                                  r.qty, "spare" if r.spare else ""),
                               tags=("spare",) if r.spare else ())
            state["items"].setdefault(r.part_id.lower(), []).append(item)
        s = parts.summary(spares.get())
        text = "%d pieces detected: %d different parts" % (s["pieces"], s["parts"])
        if s["colours"]:
            text += " in %d colours" % s["colours"]
        text += " (%d lines in the list)" % s["lines"]
        if s["spare_lines"] and not spares.get():
            text += ", spares not included"
        summary_var.set(text)

    def choose_csv():
        path = filedialog.askopenfilename(
            title="Choose a parts list", initialdir=settings.get("csv_dir") or os.path.expanduser("~"),
            filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt *.tsv"), ("All files", "*.*")])
        if not path:
            return
        try:
            parts = read_parts_csv(path)
        except ToolError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        except Exception as exc:
            messagebox.showerror(APP_NAME, "Couldn't read this file:\n%s" % exc)
            return
        state["parts"] = parts
        csv_var.set(path)
        settings["csv_dir"] = os.path.dirname(path)
        save_settings(settings)
        has_spare = "spare" in parts.columns
        spare_cb.state(["!disabled"] if has_spare else ["disabled"])
        colour_cb.state(["!disabled"] if "color" in parts.columns else ["disabled"])
        if not out_var.get():
            stem = os.path.splitext(os.path.basename(path))[0]
            out_var.set(os.path.join(os.path.dirname(path), stem + " STLs"))
        refresh_table()
        found = ", ".join("%s = '%s'" % (k, v) for k, v in parts.columns.items() if k != "spare")
        status_var.set("Columns used: %s." % found + ("  " + parts.warnings[0] if parts.warnings else ""))
        open_btn.state(["disabled"])
        progress["value"] = 0

    def choose_out():
        path = filedialog.askdirectory(title="Choose where to save the STLs",
                                       initialdir=out_var.get() or os.path.expanduser("~"))
        if path:
            out_var.set(os.path.normpath(path))

    def choose_library():
        path = filedialog.askopenfilename(title="Choose the LDraw library zip (complete.zip)",
                                          filetypes=[("Zip files", "*.zip"), ("All files", "*.*")])
        if not path:
            return
        try:
            LDrawLibrary().add_zip(path, "official")
        except ToolError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        settings["library_zip"] = path
        save_settings(settings)
        library_var.set(library_status_text(settings))

    def start():
        if state["worker"] and state["worker"].is_alive():
            return
        if state["parts"] is None:
            messagebox.showinfo(APP_NAME, "Choose a parts list first.")
            return
        out = out_var.get().strip()
        if not out:
            messagebox.showinfo(APP_NAME, "Choose an output folder first.")
            return
        settings["out_dir"] = out
        save_settings(settings)
        refresh_table()
        run_btn.state(["disabled"])
        lib_btn.state(["disabled"])
        open_btn.state(["disabled"])
        progress["value"] = 0

        def emit(kind, *payload):
            q.put((kind, payload))

        def work():
            try:
                run_job(state["parts"], out, by_colour.get(), spares.get(), unofficial.get(), emit, settings)
            except ToolError as exc:
                emit("error", str(exc))
            except Exception as exc:
                emit("error", "Something went wrong:\n%s: %s" % (type(exc).__name__, exc))

        state["worker"] = threading.Thread(target=work, daemon=True)
        state["worker"].start()

    def open_out():
        path = out_var.get()
        if not os.path.isdir(path):
            return
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import subprocess
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "status":
                    status_var.set(payload[0])
                elif kind == "progress":
                    progress["value"] = payload[0]
                elif kind == "part":
                    pid, text, ok = payload
                    for item in state["items"].get(pid, []):
                        tree.set(item, "result", text)
                        if not ok:
                            tree.item(item, tags=("bad",))
                elif kind == "done":
                    s = payload[0]
                    msg = "Done: %d STL files for %d parts in %s." % (s["files"], s["parts"], s["out"])
                    if s["failed"]:
                        msg += "  %d part(s) not found or failed, see parts_report.csv." % s["failed"]
                    status_var.set(msg)
                    library_var.set(library_status_text(settings))
                    run_btn.state(["!disabled"])
                    lib_btn.state(["!disabled"])
                    open_btn.state(["!disabled"])
                elif kind == "error":
                    status_var.set("Stopped.")
                    run_btn.state(["!disabled"])
                    lib_btn.state(["!disabled"])
                    progress["value"] = 0
                    messagebox.showerror(APP_NAME, payload[0])
        except queue.Empty:
            pass
        root.after(100, poll)

    root.after(100, poll)
    root.mainloop()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def run_cli(args) -> int:
    settings = load_settings()
    if args.library:
        settings = dict(settings, library_zip=args.library)

    def emit(kind, *payload):
        if kind == "status":
            print(payload[0], flush=True)
        elif kind == "part":
            print("  %-10s %s" % (payload[0], payload[1]), flush=True)
        elif kind == "done":
            s = payload[0]
            print("Done: %d STL files for %d parts in %s (%d not found/failed)"
                  % (s["files"], s["parts"], s["out"], s["failed"]), flush=True)

    try:
        parts = read_parts_csv(args.csv)
        s = parts.summary(args.include_spares)
        print("%d pieces detected: %d different parts in %d colours, %d lines (columns: %s)"
              % (s["pieces"], s["parts"], s["colours"], s["lines"],
                 ", ".join("%s='%s'" % kv for kv in parts.columns.items())))
        run_job(parts, args.out, not args.flat, args.include_spares, not args.no_unofficial, emit, settings)
    except ToolError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME + ": turn a LEGO parts list (CSV) into STL files.")
    parser.add_argument("--csv", help="parts list CSV (omit to open the window)")
    parser.add_argument("--out", help="output folder")
    parser.add_argument("--library", help="use this LDraw library zip instead of downloading one")
    parser.add_argument("--flat", action="store_true", help="don't sort into colour folders")
    parser.add_argument("--include-spares", action="store_true", help="include spare parts")
    parser.add_argument("--no-unofficial", action="store_true", help="don't search unofficial LDraw parts")
    args = parser.parse_args(argv)
    if args.csv:
        if not args.out:
            parser.error("--out is required with --csv")
        return run_cli(args)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
