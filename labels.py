"""
Pack labels for New Store Kits, printed on the Zebra ZD420d connected to the
server machine (direct thermal, speaks ZPL). Each pack gets a LANDSCAPE
label (the 100 x 150 mm stock read with its long side across - see
LABEL_ORIENTATION), every line centred, in Arial Black:

                Store 12                  <- header: the kit name, exactly as typed, big
                 Pack 3                   <- subheader
    ----------------------------------
       J456781-02, 05, 06, 08 & 09        <- body: the pack's item lines, exactly as
              Window decals                  on screen and in the Pack Log Sheet

The label stock, the printer's Windows name and the font file are only known
for sure once someone is at the server machine, so they come from .env
(config.load_label_config): until then the standard 100 x 150 mm courier
label and Windows' own Arial Black (C:\\Windows\\Fonts\\ariblk.ttf) are
assumed, and with no printer name set, Print opens a preview page
(GET /session/<id>/labels) instead of printing.

Arial Black isn't a font the Zebra has, so the label is DRAWN here, on the
server, with that .ttf (Pillow - already used for thumbnails) and sent to the
printer as one image (a ZPL ^GF graphic). That also means text is measured
exactly, not guessed, and the preview page shows the very image the printer
gets. If the font file can't be loaded, labels are drawn with Pillow's
default font instead (or, if even that fails, the printer's own built-in
ZPL font 0, centred by the printer itself, with a conservative width
estimate), and a startup warning says so.

ONE layout model feeds every output: layout() places every line in printer
dots, then to_zpl() turns that into the printer's language and to_html()
into the preview - line breaks included. A pack always gets exactly ONE
label: the fonts shrink until everything fits (see layout). Wrapping is always
done here, never by the printer: ZPL's own ^FB wrapping silently prints over
itself (or drops lines) when text doesn't fit, which on a box label nobody
notices until the box is on a truck.

Printing goes straight to the Windows print spooler as a RAW job
(send_raw - ctypes on winspool.drv, so no extra pip dependency): the ZPL
reaches the printer untouched, whatever its Windows driver would otherwise
do to it.

Command line (run on the server, from this folder):
    py labels.py --list-printers        what Windows calls each installed printer
    py labels.py --sample-zpl           a sample label's ZPL, printed here (no printing)
    py labels.py --sample-png FILE      the sample label drawn to a PNG (no printing)
    py labels.py --test-print [--printer NAME] [--output-file PATH]
                                        sends that sample label to the printer
"""
import argparse
import base64
import html
import io
import math
import os
import sys

import kits
from config import load_label_config


class LabelPrintError(Exception):
    """A label couldn't be printed. The message is written for people - the
    kit page shows it as-is."""


# Font heights: (share of the label's height AS READ, smallest mm, largest
# mm). The header and "Pack N" are big on purpose - they're read off a box
# across a warehouse floor - while the item lines only need to be read up
# close, so they stay small enough for a typical pack (a few series) to fit
# on ONE label: on the 100 x 150 mm courier label, landscape (100 mm tall as
# read), that's 14 / 9 / 5 mm. The mm limits keep small stock legible and
# large stock from ballooning.
HEADER_SIZE = (0.14, 4.0, 18.0)
SUBHEADER_SIZE = (0.09, 3.0, 12.0)
BODY_SIZE = (0.05, 2.5, 6.0)

# Line advance as a multiple of the font height.
HEADER_LINE_SPACING = 1.1
BODY_LINE_SPACING = 1.25

# A long kit name wraps onto at most this many header lines, shrinking the
# header font (never below the subheader's size) until it fits.
HEADER_MAX_LINES = 2

# The header stays on ONE line when that only needs the font shrunk to this
# share of its full size - "Chadstone 12 POS" reads better on one line a
# little smaller than as "Chadstone 12" over a lonely "POS".
HEADER_ONE_LINE_MIN = 0.7

# A TrueType font's size is its em; a block's height `h` here is the whole
# line cell (as in ZPL). Arial Black's capitals are ~0.72 em tall and its
# descenders ~0.21 em deep, so drawing at em = h with the baseline 0.78 h
# below the block's top keeps the ink between ~0.06 h and ~0.99 h.
TTF_BASELINE = 0.78

# Fallback only (the printer's built-in font 0): width of one character as
# a share of the font's width setting. Real glyphs average nearer 0.5; 0.62
# covers wide letters like W and M, so an estimated fit is a real fit.
CHAR_WIDTH_FACTOR = 0.62

# Fallback only: bold = the same built-in font drawn 10% wider (ZPL has no
# bold switch). Arial Black is heavy on its own and isn't widened.
BOLD_WIDTH_FACTOR = 1.1

# ZPL's scalable font refuses heights below 10 dots.
MIN_FONT_DOTS = 10

# Rule under the header: 0.4 mm thick.
RULE_THICKNESS_MM = 0.4

# The sample label behind --sample-zpl / --sample-png / --test-print (and the
# tests): the user's own example of how a pack's items read.
SAMPLE_KIT_NAME = "Store 12"
SAMPLE_PACK_NUMBER = 3
SAMPLE_ITEMS = (
    ("J456781", "02"), ("J456781", "05"), ("J456781", "06"), ("J456781", "08"), ("J456781", "09"),
    ("Window decals", ""),
)


# --- Content -----------------------------------------------------------------

def label_content(kit_name, pack_number, items):
    """What goes on one pack's label, before any layout: the header - the
    kit name exactly as it was typed (nothing is added to it: whatever
    keyword belongs at the end, "NSK" or anything else, is part of the name
    the packers give the kit) - the subheader "Pack N", and the body -
    kits.pack_lines(items), the very lines the pack's card and the Sheet
    show ("J456781-02, 05, 06, 08 & 09"; each free-text description on its
    own line)."""
    return {
        "header": str(kit_name),
        "subheader": f"Pack {pack_number}",
        "body": kits.pack_lines(items),
    }


def sample_content():
    items = []
    for i, (series_raw, idx_raw) in enumerate(SAMPLE_ITEMS, start=1):
        series, series_norm, idx = kits.parse_item(series_raw, idx_raw)
        items.append({"id": i, "series": series, "series_norm": series_norm, "idx": idx})
    return label_content(SAMPLE_KIT_NAME, SAMPLE_PACK_NUMBER, items)


# --- Sizes and fonts ---------------------------------------------------------

def dots(mm, dpi):
    """Millimetres -> printer dots at this resolution."""
    return int(round(mm / 25.4 * dpi))


def label_size_dots(cfg):
    """(width, height, margin) of the label AS THE PRINTER SEES IT, in dots:
    width = across the print head (LABEL_WIDTH_MM), height = along the feed."""
    return dots(cfg["widthMm"], cfg["dpi"]), dots(cfg["heightMm"], cfg["dpi"]), dots(cfg["marginMm"], cfg["dpi"])


def _rotated(cfg, metrics=None):
    """True when the label is laid out landscape and turned for the printer.
    Only a drawn (TrueType) label can be turned; the printer's-own-font
    fallback always prints as the stock feeds."""
    m = metrics or metrics_for(cfg)
    return m.kind == "ttf" and cfg.get("orientation", "portrait") in ("landscape", "landscape-flipped")


def _quarter_turn(cfg, metrics=None):
    """True when landscape needs the drawn label turned 90 degrees: only on
    stock that feeds long side first (100 x 150). Stock that's already wider
    than it is long (100 x 50, 4 x 3 in) is landscape as it feeds."""
    return _rotated(cfg, metrics) and cfg["heightMm"] > cfg["widthMm"]


def design_size_mm(cfg, metrics=None):
    """(width, height) in mm of the label as it's READ - landscape always
    puts the long side across: a 100 x 150 mm label is laid out 150 wide x
    100 tall, a 100 x 50 mm one stays 100 x 50."""
    if _quarter_turn(cfg, metrics):
        return cfg["heightMm"], cfg["widthMm"]
    return cfg["widthMm"], cfg["heightMm"]


def design_size_dots(cfg, metrics=None):
    """(width, height, margin) of the label as it's read, in dots - what the
    layout works in."""
    w_mm, h_mm = design_size_mm(cfg, metrics)
    return dots(w_mm, cfg["dpi"]), dots(h_mm, cfg["dpi"]), dots(cfg["marginMm"], cfg["dpi"])


def _font_width(height, bold):
    """ZPL font 0's width setting (fallback only)."""
    return int(round(height * BOLD_WIDTH_FACTOR)) if bold else int(height)


class TrueTypeMetrics:
    """Measures and draws with a .ttf file (Arial Black by default) - text
    widths are exact, so a line is wrapped exactly where it stops fitting."""
    kind = "ttf"

    def __init__(self, path):
        from PIL import ImageFont
        self.path = path
        self._fonts = {}
        self._truetype = ImageFont.truetype
        font = self.font(40)  # raises if the file isn't a usable font
        self.name = " ".join(part for part in font.getname() if part and part != "Regular")

    def font(self, height):
        height = max(1, int(height))
        if height not in self._fonts:
            self._fonts[height] = self._truetype(self.path, height)
        return self._fonts[height]

    def width(self, text, height, bold=False):
        return self.font(height).getlength(text)


class PillowDefaultMetrics(TrueTypeMetrics):
    """Pillow's bundled scalable font - the second choice when the .ttf can't
    be loaded, so a label is still drawn (landscape, centred) on the server."""

    def __init__(self):
        from PIL import ImageFont
        self.path = None
        self._fonts = {}
        self._load_default = ImageFont.load_default
        font = self.font(40)
        if not hasattr(font, "getlength"):
            raise OSError("Pillow has no scalable default font here")
        self.name = "Pillow's default font"

    def font(self, height):
        height = max(1, int(height))
        if height not in self._fonts:
            self._fonts[height] = self._load_default(size=height)
        return self._fonts[height]


class BuiltinFontMetrics:
    """The printer's own scalable font (ZPL font 0) - only when the .ttf
    can't be loaded. Its real glyph widths aren't known here, so widths are
    a deliberately generous estimate (see CHAR_WIDTH_FACTOR)."""
    kind = "zpl"
    name = "the printer's built-in font"

    def width(self, text, height, bold=False):
        return len(text) * CHAR_WIDTH_FACTOR * _font_width(height, bold)


_metrics_cache = {}


def metrics_for(cfg):
    """The font the labels are drawn with: cfg["fontFile"] if it loads, else
    the printer's built-in font (warned about once per file, not on every
    print). A cfg without a fontFile (tests, an older caller) means built-in."""
    path = (cfg.get("fontFile") or "").strip()
    if path not in _metrics_cache:
        metrics = None
        if path:
            try:
                if not os.path.isfile(path):
                    raise OSError("no such file")
                metrics = TrueTypeMetrics(path)
            except Exception as exc:  # noqa: BLE001 - never stop printing over a font
                try:
                    metrics = PillowDefaultMetrics()
                    instead = "Pillow's default font"
                except Exception:  # noqa: BLE001
                    instead = "the printer's built-in font"
                print(f"[labels] WARNING: label font {path!r} couldn't be loaded ({exc}) - "
                      f"using {instead} instead (see LABEL_FONT_FILE in .env).")
        _metrics_cache[path] = metrics or BuiltinFontMetrics()
    return _metrics_cache[path]


def font_description(cfg):
    """ "Arial Black" / "the printer's built-in font" - for startup lines and the preview."""
    return metrics_for(cfg).name


# --- Layout ------------------------------------------------------------------

def text_width(text, font_width):
    """Built-in-font width estimate, in dots (kept for callers of the old API)."""
    return len(text) * CHAR_WIDTH_FACTOR * font_width


def block_width(block):
    """How far a laid-out block reaches to the right of its x, in dots."""
    if block["kind"] == "rule":
        return block["w"]
    if "w" in block:
        return block["w"]
    return text_width(block["text"], block["fw"])


def _tokens(text):
    """Words to wrap on. "&" sticks to the number after it, so a line never
    ends in a dangling "&" with its last number alone on the next line."""
    words = text.split()
    tokens = []
    i = 0
    while i < len(words):
        if words[i] == "&" and i + 1 < len(words):
            tokens.append(f"& {words[i + 1]}")
            i += 2
        else:
            tokens.append(words[i])
            i += 1
    return tokens


def _break_word(m, word, height, bold, avail):
    """A single word too wide for a whole line, cut into pieces that fit."""
    pieces = []
    piece = ""
    for ch in word:
        if piece and m.width(piece + ch, height, bold) > avail:
            pieces.append(piece)
            piece = ch
        else:
            piece += ch
    if piece:
        pieces.append(piece)
    return pieces or [word]


def _wrap(m, text, height, bold, avail):
    """Greedy word wrap by MEASURED width. A word wider than a whole line is
    the only thing ever broken mid-word."""
    lines = []
    line = ""
    for token in _tokens(text):
        candidate = f"{line} {token}" if line else token
        if m.width(candidate, height, bold) <= avail:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        if m.width(token, height, bold) <= avail:
            line = token
        else:
            pieces = _break_word(m, token, height, bold, avail)
            lines.extend(pieces[:-1])
            line = pieces[-1]
    if line or not lines:
        lines.append(line)
    return lines


def _font_dots(size, cfg, scale, design_height_mm=None):
    """A font height in dots from (share of the label's height as READ, min
    mm, max mm) - for a landscape 100 x 150 mm label that height is 100 mm."""
    share, lo_mm, hi_mm = size
    height_mm = cfg["heightMm"] if design_height_mm is None else design_height_mm
    mm = min(hi_mm, max(lo_mm, height_mm * share))
    return dots(mm * scale, cfg["dpi"])


def _text_block(m, plan, y, height, bold, text):
    """One centred line: x is where it starts so that it sits in the middle
    of the printable width (the built-in-font path also asks the printer to
    centre it - see to_zpl)."""
    w = m.width(text, height, bold)
    x = plan["margin"] + max(0, (plan["avail_w"] - w) / 2.0)
    return {"kind": "text", "x": int(round(x)), "y": int(y), "h": int(height),
            "fw": _font_width(height, bold), "bold": bool(bold), "text": text, "w": float(w)}


def _plan(m, content, cfg, scale, body_scale=1.0):
    """One attempt at a layout at `scale` x the normal font sizes: the font
    sizes, the header's lines and the body's wrapped lines (a list per item
    line), as a dict - or None if the header and subheader alone don't fit
    on the label at this scale."""
    width, height, margin = design_size_dots(cfg, m)
    avail_w = width - 2 * margin
    bottom = height - margin
    design_h_mm = design_size_mm(cfg, m)[1]

    sub_base = max(MIN_FONT_DOTS, _font_dots(SUBHEADER_SIZE, cfg, scale, design_h_mm))
    header_h = max(sub_base, _font_dots(HEADER_SIZE, cfg, scale, design_h_mm))
    body_h = max(MIN_FONT_DOTS, int(round(_font_dots(BODY_SIZE, cfg, scale, design_h_mm) * body_scale)))

    # Header: one line if a modest shrink (down to HEADER_ONE_LINE_MIN of the
    # full size) makes it fit. Otherwise shrink until it wraps onto
    # HEADER_MAX_LINES lines without breaking a word, but never below the
    # subheader's size - past that, a very long kit name gets more lines
    # rather than shrinking into the small print (or being cut off: the name
    # is what the box is found by).
    header = content["header"]
    full_h = header_h
    one_line_floor = max(sub_base, int(round(full_h * HEADER_ONE_LINE_MIN)))
    header_lines = None
    h = full_h
    while h >= one_line_floor:
        if m.width(header, h, True) <= avail_w:
            header_lines, header_h = [header], h
            break
        h -= max(1, int(round(h * 0.04)))
    tokens = _tokens(header)
    while header_lines is None:
        lines = _wrap(m, header, header_h, True, avail_w)
        words_fit = all(m.width(t, header_h, True) <= avail_w for t in tokens)
        if (len(lines) <= HEADER_MAX_LINES and words_fit) or header_h <= sub_base:
            header_lines = lines
            break
        header_h = max(sub_base, header_h - max(1, int(round(header_h * 0.04))))

    # Body lines, wrapped by measured width; every line (continuations too)
    # is centred, so a continuation needs no indent to read as one item.
    wrapped = [_wrap(m, line, body_h, False, avail_w) for line in content["body"]]

    header_adv = int(round(header_h * HEADER_LINE_SPACING))
    header_bottom = margin + header_adv * (len(header_lines) - 1) + header_h
    sub_top = header_bottom + int(round(sub_base * 0.25))
    if sub_top + sub_base > bottom:
        return None
    return {
        "width": width, "height": height, "margin": margin, "avail_w": avail_w, "bottom": bottom,
        "header_lines": header_lines, "header_h": header_h, "sub_base": sub_base, "sub_top": sub_top,
        "body_h": body_h, "wrapped": wrapped,
    }


def _subheader_height(m, plan, text):
    """The subheader is one line: normally the subheader size, smaller only
    if "Pack 12 (2/3)" wouldn't fit across a narrow label."""
    sub_h = plan["sub_base"]
    while sub_h > MIN_FONT_DOTS and m.width(text, sub_h, False) > plan["avail_w"]:
        sub_h -= 1
    return sub_h


def _page_frame(m, plan, subheader_text):
    """Header lines, subheader and rule for one page -> (blocks, body_top).
    Everything below the subheader is placed from the subheader's normal
    size, so it lands in the same place on every page of a label."""
    margin = plan["margin"]
    blocks = []
    header_adv = int(round(plan["header_h"] * HEADER_LINE_SPACING))
    for i, line in enumerate(plan["header_lines"]):
        blocks.append(_text_block(m, plan, margin + i * header_adv, plan["header_h"], True, line))
    sub_h = _subheader_height(m, plan, subheader_text)
    blocks.append(_text_block(m, plan, plan["sub_top"], sub_h, False, subheader_text))
    rule_y = plan["sub_top"] + plan["sub_base"] + int(round(plan["sub_base"] * 0.2))
    blocks.append({"kind": "rule", "x": margin, "y": rule_y, "w": plan["avail_w"], "h": plan["rule_t"]})
    body_top = rule_y + plan["rule_t"] + int(round(plan["body_h"] * 0.5))
    return blocks, body_top


def _capacity(plan, body_top):
    """How many body lines fit below body_top."""
    room = plan["bottom"] - body_top
    if room < plan["body_h"]:
        return 0
    return 1 + (room - plan["body_h"]) // int(round(plan["body_h"] * BODY_LINE_SPACING))


# How far the item lines may shrink (as a share of their normal size) before
# the header and "Pack N" start shrinking too - the item lines are read up
# close, the header is what finds the box across the floor.
BODY_MIN_SCALE = 0.45

# The smallest the header/subheader are allowed to get, as a share of normal.
FRAME_MIN_SCALE = 0.5


def _fits(m, content, cfg, scale, body_scale):
    """(plan, frame, body_top) if everything fits on ONE label at these
    scales, else None."""
    plan = _plan(m, content, cfg, scale, body_scale)
    if plan is None:
        return None
    plan["rule_t"] = max(2, dots(RULE_THICKNESS_MM, cfg["dpi"]))
    frame, body_top = _page_frame(m, plan, content["subheader"])
    rule = frame[-1]
    if rule["y"] + rule["h"] > plan["bottom"]:
        return None
    lines = sum(len(group) for group in plan["wrapped"])
    if lines > _capacity(plan, body_top):
        return None
    return plan, frame, body_top


def layout(content, cfg, metrics=None):
    """Lays one pack's label out on ONE label (a pack never gets a second
    label - there's one box, so one label): a list holding one page, a list
    of blocks in dots on the label AS IT'S READ (landscape: the long side
    across - see design_size_dots), origin top-left, every text line centred:

        {"kind": "text", "x", "y", "h": line height, "fw": built-in font width,
         "bold", "text", "w": measured width}
        {"kind": "rule", "x", "y", "w", "h": thickness}

    Fonts adjust themselves so everything fits: the item lines shrink first
    (down to BODY_MIN_SCALE of their normal size), then the header and "Pack
    N" with them (down to FRAME_MIN_SCALE), then the item lines on down to
    the smallest the printer can print. Only if even that can't hold every
    line does the last line become "+ N more line(s)" - the pack's card and
    the Pack Log Sheet still list them all. Raises LabelPrintError if the
    configured label is too small to hold even the header."""
    m = metrics or metrics_for(cfg)
    attempts = []
    body_scale = 1.0
    while body_scale >= BODY_MIN_SCALE - 1e-9:
        attempts.append((1.0, body_scale))
        body_scale *= 0.93
    scale = 0.93
    while scale >= FRAME_MIN_SCALE - 1e-9:
        attempts.append((scale, BODY_MIN_SCALE))
        scale *= 0.93
    tiny = BODY_MIN_SCALE
    while tiny > 0.05:
        tiny *= 0.9
        attempts.append((FRAME_MIN_SCALE, tiny))

    fitted = None
    for scale, body_scale in attempts:
        fitted = _fits(m, content, cfg, scale, body_scale)
        if fitted is not None:
            break

    if fitted is None:
        # Too much for any legible size: smallest fonts, and the lines that
        # don't fit summarised in a last line.
        plan = None
        for scale in (FRAME_MIN_SCALE, 0.35, 0.25, 0.2):
            plan = _plan(m, content, cfg, scale, attempts[-1][1] / FRAME_MIN_SCALE * scale)
            if plan is not None:
                break
        if plan is None:
            raise LabelPrintError(
                "The label is too small for its text - check LABEL_WIDTH_MM / LABEL_HEIGHT_MM in .env."
            )
        plan["rule_t"] = max(2, dots(RULE_THICKNESS_MM, cfg["dpi"]))
        frame, body_top = _page_frame(m, plan, content["subheader"])
        capacity = max(1, _capacity(plan, body_top))
        lines = [line for group in plan["wrapped"] for line in group]
        keep = lines[:capacity - 1]
        more = len(lines) - len(keep)
        body_lines = keep + [f"+ {more} more line{'' if more == 1 else 's'}"]
    else:
        plan, frame, body_top = fitted
        body_lines = [line for group in plan["wrapped"] for line in group]

    blocks = list(frame)
    advance = int(round(plan["body_h"] * BODY_LINE_SPACING))
    for i, line in enumerate(body_lines):
        blocks.append(_text_block(m, plan, body_top + i * advance, plan["body_h"], False, line))
    return [blocks]


# --- Drawing (TrueType path) -----------------------------------------------------

def render_page(page, cfg, metrics=None):
    """One label drawn as a 1-bit image (mode "1": 0 = black ink) the way
    it's READ (landscape: long side across) - what the preview shows;
    printer_image() turns it for the printer. TrueType path only."""
    from PIL import Image, ImageDraw
    m = metrics or metrics_for(cfg)
    width, height, _ = design_size_dots(cfg, m)
    img = Image.new("1", (width, height), 1)
    draw = ImageDraw.Draw(img)
    for b in page:
        if b["kind"] == "rule":
            draw.rectangle([b["x"], b["y"], b["x"] + b["w"] - 1, b["y"] + b["h"] - 1], fill=0)
        else:
            baseline = b["y"] + int(round(b["h"] * TTF_BASELINE))
            draw.text((b["x"], baseline), b["text"], font=m.font(b["h"]), fill=0, anchor="ls")
    return img


def printer_image(page, cfg, metrics=None):
    """render_page turned to how the stock feeds through the printer: its
    width is the print-head width (^PW). On tall stock landscape turns the
    drawn label 90 degrees clockwise, "landscape-flipped" 90 degrees the
    other way; on already-wide stock landscape needs no turn and
    "landscape-flipped" turns it upside down."""
    from PIL import Image
    m = metrics or metrics_for(cfg)
    img = render_page(page, cfg, m)
    if _quarter_turn(cfg, m):
        turn = Image.Transpose.ROTATE_270 if cfg.get("orientation") == "landscape" else Image.Transpose.ROTATE_90
        img = img.transpose(turn)
    elif _rotated(cfg, m) and cfg.get("orientation") == "landscape-flipped":
        img = img.transpose(Image.Transpose.ROTATE_180)
    width, height, _ = label_size_dots(cfg)
    if img.size != (width, height):  # 1-dot rounding between the two sides' mm -> dots
        fitted = Image.new("1", (width, height), 1)
        fitted.paste(img, (0, 0))
        img = fitted
    return img


def _gf_data(img):
    """The image as ^GFA hex rows, black = 1 bits, using ZPL's standard
    row compression: a row that ends in blank bytes is cut short with ","
    ("rest of this row is blank"), and a row identical to the one above is
    just ":" ("repeat the previous row"). A mostly-white label shrinks from
    ~240 KB of hex to a few tens of KB. Returns (bytes_per_row, total_bytes, data)."""
    width, height = img.size
    bytes_per_row = (width + 7) // 8
    # Pillow packs mode "1" rows MSB-first with 1 = white; ZPL wants 1 = black.
    raw = img.tobytes()
    rows = []
    previous = None
    for y in range(height):
        row = bytes(b ^ 0xFF for b in raw[y * bytes_per_row:(y + 1) * bytes_per_row])
        if width % 8:  # the padding bits past the image's right edge stay white
            row = row[:-1] + bytes([row[-1] & (0xFF << (8 - width % 8)) & 0xFF])
        if row == previous:
            rows.append(":")
            continue
        previous = row
        stripped = row.rstrip(b"\x00")
        text = stripped.hex().upper()
        rows.append(text if len(stripped) == len(row) else text + ",")
    return bytes_per_row, bytes_per_row * height, "".join(rows)


def decode_gf_data(data, bytes_per_row, rows):
    """Inverse of _gf_data's compression (",", ":" and plain hex) -> the row
    bytes. For the tests - and anyone checking what the printer is sent."""
    out = []
    i = 0
    previous = bytes(bytes_per_row)
    while len(out) < rows:
        if data[i] == ":":
            out.append(previous)
            i += 1
            continue
        hex_digits = []
        while i < len(data) and data[i] not in ",:" and len(hex_digits) < bytes_per_row * 2:
            hex_digits.append(data[i])
            i += 1
        if i < len(data) and data[i] == ",":
            i += 1
        row = bytes.fromhex("".join(hex_digits)).ljust(bytes_per_row, b"\x00")
        out.append(row)
        previous = row
    return out


# --- Output: ZPL for the printer, HTML for the preview -------------------------

def zpl_escape(text):
    """Field data for ^FH_ (built-in-font path): ZPL's command characters
    (^ ~), its escape character (_), backslash, control characters and every
    byte of a non-ASCII character (UTF-8, per ^CI28 - "Café", "Łódź") are
    written as _XX hex, so no kit name or item text can ever end a field
    early or be read as a printer command."""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x20 <= code < 0x7F and ch not in "^~_\\":
            out.append(ch)
        else:
            out.append("".join(f"_{b:02X}" for b in ch.encode("utf-8")))
    return "".join(out)


def to_zpl(pages, cfg, metrics=None):
    """The pages as ZPL, one ^XA...^XZ format per label. ^PW/^LL = the
    label's width/length in dots; ^LH0,0 = no label home offset.
    Deliberately no media type, darkness or speed commands - those stay
    whatever the printer itself is set up for (its own calibration knows its
    stock better than a guess here).

    TrueType font (Arial Black): the whole label is one ^GFA graphic
    (printer_image - drawn landscape, turned for the feed) - no text reaches
    the printer as text, so nothing in a kit name can be read as a command.
    Built-in font (last-resort fallback, portrait only): one centred ^FB
    field per line (^CI28 UTF-8 field data, every special byte ^FH_-escaped)."""
    m = metrics or metrics_for(cfg)
    width, height, _ = label_size_dots(cfg)
    out = []
    for page in pages:
        if m.kind == "ttf":
            out += ["^XA", f"^PW{width}", f"^LL{height}", "^LH0,0"]
            bytes_per_row, total, data = _gf_data(printer_image(page, cfg, m))
            out.append(f"^FO0,0^GFA,{total},{total},{bytes_per_row},{data}^FS")
        else:
            out += ["^XA", "^CI28", f"^PW{width}", f"^LL{height}", "^LH0,0"]
            for b in page:
                if b["kind"] == "rule":
                    out.append(f"^FO{b['x']},{b['y']}^GB{b['w']},{b['h']},{b['h']}^FS")
                else:
                    # centred by the printer across the printable width (one line - wrapping is done here)
                    _, _, margin = label_size_dots(cfg)
                    left, avail = margin, width - 2 * margin
                    out.append(
                        f"^FO{left},{b['y']}^FB{avail},1,0,C,0^A0N,{b['h']},{b['fw']}"
                        f"^FH_^FD{zpl_escape(b['text'])}^FS"
                    )
        out += ["^PQ1", "^XZ"]
    return "\n".join(out) + "\n"


def _mm(value_dots, cfg):
    return f"{value_dots * 25.4 / cfg['dpi']:.3f}mm"


def _fmt_number(value):
    return f"{value:g}"


def page_png(page, cfg, metrics=None):
    """One label (TrueType path) as PNG bytes - the preview's image."""
    buf = io.BytesIO()
    render_page(page, cfg, metrics).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def pdf_metrics(cfg):
    """The font a PDF of the labels is drawn with: the label font when it
    draws (Arial Black, or Pillow's font if that didn't load); a PDF can't
    use the printer's own built-in font, so that case draws with Pillow's."""
    m = metrics_for(cfg)
    return m if m.kind == "ttf" else PillowDefaultMetrics()


def pdf_bytes(pages, cfg, metrics=None):
    """The labels as ONE PDF, one page per label, each page exactly the
    label's size as it's read (landscape: 150 x 100 mm on the courier
    stock) - the very images the printer gets (render_page), so the PDF in
    Drive looks like the boxes do. `pages` must be laid out with the same
    metrics (see pdf_metrics). 1-bit images, CCITT-compressed by Pillow:
    a couple of KB per label."""
    m = metrics or pdf_metrics(cfg)
    if not pages:
        raise LabelPrintError("No labels to put in the PDF.")
    images = [render_page(page, cfg, m) for page in pages]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:], resolution=float(cfg["dpi"]),
                   title="Pack labels", producer="Campaign Photo Portal")
    return buf.getvalue()


def kit_labels_pdf(kit_name, packs, cfg=None):
    """PDF bytes for a whole kit: `packs` = [(pack_number, items), ...] in
    pack order, one label each (layout always gives one), with the label
    stock and font from .env - the same labels Print would give."""
    cfg = cfg or load_label_config()
    m = pdf_metrics(cfg)
    pages = []
    for number, items in packs:
        pages.extend(layout(label_content(kit_name, number, items), cfg, m))
    return pdf_bytes(pages, cfg, m)


def to_html(pages, cfg, metrics=None):
    """The same pages for the preview page: each label a <div> of the
    label's real size (mm), one printed page per label (@page sized to the
    label, no margins) - so printing the preview from a browser onto the
    same stock gives the same label. With the TrueType font each label IS the
    printer's image (a PNG of render_page); with the built-in font the lines
    are positioned (centred) text in a similar face."""
    m = metrics or metrics_for(cfg)
    design_w, design_h = design_size_mm(cfg, m)
    w_mm, h_mm = _fmt_number(design_w), _fmt_number(design_h)
    out = [
        "<style>",
        f"@page {{ size: {w_mm}mm {h_mm}mm; margin: 0; }}",
        f".nsk-label {{ position: relative; width: {w_mm}mm; height: {h_mm}mm; overflow: hidden;"
        " background: #fff; color: #000; box-sizing: border-box;"
        " page-break-after: always; break-after: page; }",
        ".nsk-label:last-child { page-break-after: auto; break-after: auto; }",
        ".nsk-img { display: block; width: 100%; height: 100%; image-rendering: pixelated; }",
        ".nsk-t { position: absolute; margin: 0; white-space: pre; line-height: 1; text-align: center;"
        ' font-family: "Arial Narrow", "Roboto Condensed", "Helvetica Neue", Arial, sans-serif;'
        " font-stretch: condensed; font-weight: 600; }",
        ".nsk-t.nsk-b { font-weight: 800; }",
        ".nsk-rule { position: absolute; background: #000; }",
        "</style>",
    ]
    _, _, margin = label_size_dots(cfg)
    width, _, _ = label_size_dots(cfg)
    for n, page in enumerate(pages, start=1):
        out.append(f'<div class="nsk-label" data-label="{n}">')
        if m.kind == "ttf":
            src = "data:image/png;base64," + base64.b64encode(page_png(page, cfg, m)).decode("ascii")
            out.append(f'<img class="nsk-img" src="{src}" alt="Label {n}">')
        else:
            for b in page:
                if b["kind"] == "rule":
                    pos = f"left:{_mm(b['x'], cfg)};top:{_mm(b['y'], cfg)}"
                    out.append(
                        f'<div class="nsk-rule" style="{pos};width:{_mm(b["w"], cfg)};height:{_mm(b["h"], cfg)}"></div>'
                    )
                else:
                    cls = "nsk-t nsk-b" if b["bold"] else "nsk-t"
                    pos = f"left:{_mm(margin, cfg)};width:{_mm(width - 2 * margin, cfg)};top:{_mm(b['y'], cfg)}"
                    out.append(
                        f'<div class="{cls}" style="{pos};font-size:{_mm(b["h"], cfg)}">{html.escape(b["text"])}</div>'
                    )
        out.append("</div>")
    return "\n".join(out)


def size_text(cfg):
    """ "100x150mm @203dpi, landscape" """
    orientation = cfg.get("orientation")
    suffix = f", {orientation}" if orientation else ""
    return f"{_fmt_number(cfg['widthMm'])}x{_fmt_number(cfg['heightMm'])}mm @{cfg['dpi']}dpi{suffix}"


# --- Windows print spooler (winspool.drv via ctypes) ---------------------------

_PRINTER_ENUM_LOCAL = 0x2
_PRINTER_ENUM_CONNECTIONS = 0x4
_ERROR_INSUFFICIENT_BUFFER = 122
_WRITE_CHUNK = 64 * 1024

_winspool_lib = None
_structs_cache = None


def _structs():
    """(DOC_INFO_1W, PRINTER_INFO_4W) ctypes structures, built on first use
    so importing this module never needs Windows."""
    global _structs_cache
    if _structs_cache is None:
        import ctypes
        from ctypes import wintypes

        class DOC_INFO_1W(ctypes.Structure):
            _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR),
                        ("pDatatype", wintypes.LPWSTR)]

        class PRINTER_INFO_4W(ctypes.Structure):
            _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pServerName", wintypes.LPWSTR),
                        ("Attributes", wintypes.DWORD)]

        _structs_cache = (DOC_INFO_1W, PRINTER_INFO_4W)
    return _structs_cache


def _winspool():
    """winspool.drv with every function this module calls declared
    (argtypes/restype), loaded once. A test swaps this function for one
    returning a fake, so nothing reaches a real printer."""
    global _winspool_lib
    if _winspool_lib is None:
        if sys.platform != "win32":
            raise LabelPrintError("Label printing needs the Windows server.")
        import ctypes
        from ctypes import wintypes

        doc_info, _ = _structs()
        lib = ctypes.WinDLL("winspool.drv", use_last_error=True)
        handle = wintypes.HANDLE
        signatures = {
            "OpenPrinterW": ([wintypes.LPWSTR, ctypes.POINTER(handle), ctypes.c_void_p], wintypes.BOOL),
            "StartDocPrinterW": ([handle, wintypes.DWORD, ctypes.POINTER(doc_info)], wintypes.DWORD),
            "StartPagePrinter": ([handle], wintypes.BOOL),
            "WritePrinter": ([handle, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
                             wintypes.BOOL),
            "EndPagePrinter": ([handle], wintypes.BOOL),
            "EndDocPrinter": ([handle], wintypes.BOOL),
            "AbortPrinter": ([handle], wintypes.BOOL),
            "ClosePrinter": ([handle], wintypes.BOOL),
            "EnumPrintersW": ([wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        }
        for name, (argtypes, restype) in signatures.items():
            fn = getattr(lib, name)
            fn.argtypes = argtypes
            fn.restype = restype
        _winspool_lib = lib
    return _winspool_lib


def _last_error_text(what):
    """ "<what>: <Windows' own explanation> (error N)" for the last failed
    winspool call on this thread."""
    import ctypes

    code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
    if not code:
        return f"{what}."
    try:
        reason = ctypes.FormatError(code).strip().rstrip(".")
    except Exception:  # noqa: BLE001 - the code alone still helps
        reason = "Windows error"
    return f"{what}: {reason} (error {code})."


def send_raw(data, printer_name, doc_name="Pack labels", output_file=None):
    """Sends `data` (bytes - ZPL here) to the Windows printer `printer_name`
    as ONE raw spooler job and returns its job id. RAW means the spooler
    passes the bytes to the printer untouched - no driver rendering.
    output_file, if given, makes the spooler write the job to that file
    instead of the printer's port (handy for checking what would be sent).

    Every winspool failure becomes a LabelPrintError carrying Windows' own
    reason ("The printer name is invalid", "Access is denied" ...). The
    printer handle is always closed; a job that fails part-way through is
    aborted, so half a label can't sit in the queue and garble the next one."""
    import ctypes
    from ctypes import wintypes

    if not printer_name:
        raise LabelPrintError("No label printer is set up on the server (LABEL_PRINTER_NAME in .env).")
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = bytes(data)
    lib = _winspool()
    doc_info_cls, _ = _structs()

    handle = wintypes.HANDLE()
    if not lib.OpenPrinterW(printer_name, ctypes.byref(handle), None):
        raise LabelPrintError(_last_error_text(f'Couldn\'t open the label printer "{printer_name}"'))
    try:
        doc = doc_info_cls(doc_name, output_file, "RAW")
        job_id = lib.StartDocPrinterW(handle, 1, ctypes.byref(doc))
        if not job_id:
            raise LabelPrintError(_last_error_text(f'"{printer_name}" refused the print job'))
        try:
            if not lib.StartPagePrinter(handle):
                raise LabelPrintError(_last_error_text(f'"{printer_name}" refused the print job'))
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + _WRITE_CHUNK]
                buf = ctypes.create_string_buffer(chunk, len(chunk))
                written = wintypes.DWORD(0)
                if not lib.WritePrinter(handle, buf, len(chunk), ctypes.byref(written)):
                    raise LabelPrintError(_last_error_text(f'Sending the labels to "{printer_name}" failed'))
                if written.value == 0:
                    raise LabelPrintError(f'"{printer_name}" stopped accepting data - check it is on and online.')
                offset += written.value
            if not lib.EndPagePrinter(handle):
                raise LabelPrintError(_last_error_text(f'Finishing the print job on "{printer_name}" failed'))
        except BaseException:
            lib.AbortPrinter(handle)  # also ends the document
            raise
        if not lib.EndDocPrinter(handle):
            raise LabelPrintError(_last_error_text(f'Finishing the print job on "{printer_name}" failed'))
        return int(job_id)
    finally:
        lib.ClosePrinter(handle)


def list_printers():
    """Names of the printers installed on this machine (local and network
    connections) - exactly what LABEL_PRINTER_NAME has to match. [] when not
    on Windows; LabelPrintError if Windows won't say."""
    if sys.platform != "win32" and _winspool_lib is None:
        return []
    import ctypes
    from ctypes import wintypes

    lib = _winspool()
    _, info_cls = _structs()
    flags = _PRINTER_ENUM_LOCAL | _PRINTER_ENUM_CONNECTIONS
    needed = wintypes.DWORD(0)
    returned = wintypes.DWORD(0)
    # First call with no buffer just asks how big one has to be.
    if lib.EnumPrintersW(flags, None, 4, None, 0, ctypes.byref(needed), ctypes.byref(returned)):
        return []  # succeeded with an empty buffer: nothing installed
    if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not needed.value:
        raise LabelPrintError(_last_error_text("Couldn't list the installed printers"))
    buf = (ctypes.c_byte * needed.value)()
    if not lib.EnumPrintersW(flags, None, 4, buf, needed.value, ctypes.byref(needed), ctypes.byref(returned)):
        raise LabelPrintError(_last_error_text("Couldn't list the installed printers"))
    infos = ctypes.cast(buf, ctypes.POINTER(info_cls))
    return [infos[i].pPrinterName for i in range(returned.value) if infos[i].pPrinterName]


def startup_lines(cfg=None):
    """What serve.py / app.py print at startup about label printing - never
    raises (a printer problem must not stop the portal from starting)."""
    try:
        cfg = cfg or load_label_config()
        try:
            installed = list_printers()
            installed_text = ", ".join(installed) if installed else "(none found)"
        except Exception as exc:  # noqa: BLE001
            installed = None
            installed_text = f"(couldn't list them: {exc})"
        name = cfg["printerName"]
        # Loading the font here (not at the first print) is what makes a
        # missing/mistyped LABEL_FONT_FILE show up at startup.
        font = font_description(cfg)
        m = metrics_for(cfg)
        font_warning = None
        if (cfg.get("fontFile") or "").strip() and (not isinstance(m, TrueTypeMetrics) or isinstance(m, PillowDefaultMetrics)):
            font_warning = (f"[startup] WARNING: labels won't be in the chosen font - LABEL_FONT_FILE "
                            f"{cfg.get('fontFile')!r} couldn't be loaded, using {font} instead.")
        if not name:
            lines = [
                "[startup] Label printer not configured (LABEL_PRINTER_NAME in .env) - Print shows a preview "
                f"(font: {font}). Installed printers: {installed_text}"
            ]
            return lines + ([font_warning] if font_warning else [])
        lines = [f'[startup] Label printer: "{name}" ({size_text(cfg)}, font: {font})']
        if font_warning:
            lines.append(font_warning)
        if installed is not None and name.casefold() not in {p.casefold() for p in installed}:
            lines.append(
                f'[startup] WARNING: no installed printer is called "{name}" - check LABEL_PRINTER_NAME in .env. '
                f"Installed printers: {installed_text}"
            )
        return lines
    except Exception as exc:  # noqa: BLE001 - see docstring
        return [f"[startup] Label printing check failed: {exc}"]


# --- Command line ------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="New Store Kit pack labels (Zebra ZD420d, ZPL).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list-printers", action="store_true", help="list the installed printers' names")
    group.add_argument("--sample-zpl", action="store_true", help="print a sample label's ZPL (no printing)")
    group.add_argument("--sample-png", metavar="FILE", help="draw the sample label to a PNG file (no printing)")
    group.add_argument("--test-print", action="store_true", help="send the sample label to the printer")
    parser.add_argument("--printer", help="printer name (default: LABEL_PRINTER_NAME from .env)")
    parser.add_argument("--output-file", help="with --test-print: spool the job to this file instead")
    args = parser.parse_args(argv)

    cfg = load_label_config()
    if args.list_printers:
        try:
            names = list_printers()
        except LabelPrintError as exc:
            print(exc)
            return 1
        for name in names:
            marker = "  <- LABEL_PRINTER_NAME" if name.casefold() == cfg["printerName"].casefold() else ""
            print(f"{name}{marker}")
        if not names:
            print("(no printers found)")
        return 0

    pages = layout(sample_content(), cfg)
    if args.sample_png:
        m = metrics_for(cfg)
        if m.kind != "ttf":
            print(f"The label font ({cfg['fontFile']}) couldn't be loaded, so labels use the printer's own font "
                  "and can't be drawn here - see LABEL_FONT_FILE in .env.")
            return 1
        with open(args.sample_png, "wb") as f:
            f.write(page_png(pages[0], cfg, m))
        print(f"Wrote {args.sample_png} ({size_text(cfg)}, {m.name}).")
        return 0
    zpl = to_zpl(pages, cfg)
    if args.sample_zpl:
        sys.stdout.write(zpl)
        return 0

    printer = args.printer or cfg["printerName"]
    if not printer:
        print("No printer given - set LABEL_PRINTER_NAME in .env or pass --printer NAME "
              "(see: py labels.py --list-printers).")
        return 1
    try:
        job_id = send_raw(zpl.encode("utf-8"), printer, doc_name="Pack labels - test label", output_file=args.output_file)
    except LabelPrintError as exc:
        print(exc)
        return 1
    print(f'Sent {len(pages)} sample label(s) to "{printer}" ({size_text(cfg)}) - spooler job {job_id}.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
