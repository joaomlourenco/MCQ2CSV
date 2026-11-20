#!/usr/bin/env python3
"""Read scanned Akindi answer sheets (1 to 50 questions, A-D) into a CSV.

Each page is aligned on its four corner squares, then every bubble is
classified as filled / faint / empty, and checked for being crossed out
or circled.  Rows the rules can settle are decided and logged in
decisions.csv; the rest are written to review.csv (with a picture of the
row) and left as "?" in answers.csv until a line in overrides.csv
settles them.  review.py serves a page for making those decisions.

Usage:  python3 mcq2csv.py SCANS.pdf [more.pdf ...] [-o OUTDIR] [--roster inscritos.txt]
                         [--questions N] [--debug]
Needs:  numpy, Pillow, and poppler's pdftoppm for PDF input.
"""
import argparse
import csv
import itertools
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

DPI = 200
PAGE_W, PAGE_H = 1654, 2339        # A4 in pixels at 200 dpi

# Centres of the corner squares on the reference page (200 dpi), and their size.
REF_MARKERS = np.array([[210.8, 269.8], [1467.0, 271.1], [195.0, 2066.0], [1451.5, 2076.6]])
MARKER_SIZE = 45
CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")

# Bubble centres in the reference frame.
R = 13                             # bubble radius in pixels
MAX_Q = 50                         # 25 rows in each of the two answer columns
LETTERS = "ABCD"


def answer_centres(q):
    """Centres of the A-D bubbles of question q (0-based).

    Measured on a 30-question form; rows 31-50 continue the second column at
    the same spacing.
    """
    if q < 25:
        x0, y = 920.4, 417.0 + 44.45 * q
    else:
        x0, y = 1259.4, 418.0 + 44.5 * (q - 25)
    return [(x0 + 52.0 * i, y) for i in range(4)]


VERSION_CENTRES = [(211.0 + 52.0 * i, 736.0) for i in range(5)]
STUDENT_COLS = 5


def student_centres(col):
    """Centres of the 0-9 bubbles of student-number column col."""
    return [(209.5 + 51.75 * col, 884.8 + 44.3 * d) for d in range(10)]


# Crops (reference frame) used for review pictures.
STUDENT_BOX = (170, 790, 460, 1310)    # handwritten number + bubbles
VERSION_BOX = (180, 690, 450, 770)

INK = 190                          # grey level below which a pixel counts as ink
BIN_INK_PIXELS = 6                 # ink pixels needed for an angular bin to count
NBINS = 16


# ---------------------------------------------------------------- input

def load_pages(path, tmp):
    """Yield (page number, greyscale array) for a PDF or an image file."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        prefix = Path(tmp) / path.stem
        subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", str(path), str(prefix)], check=True)
        files = sorted(Path(tmp).glob(path.stem + "-*.pgm"))
    else:
        files = [path]
    for n, f in enumerate(files, 1):
        im = Image.open(f).convert("L")
        if im.width != PAGE_W:                   # near the reference size; the squares do the rest
            im = im.resize((PAGE_W, round(im.height * PAGE_W / im.width)), Image.LANCZOS)
        yield n, np.asarray(im, dtype=float)


# ---------------------------------------------------------------- alignment

def dark_blobs(mask):
    """(x0, y0, x1, y1, pixel count) of each 8-connected group of True pixels."""
    seen = np.zeros_like(mask)
    h, w = mask.shape
    blobs = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        stack, xs, ys = [(y0, x0)], [], []
        while stack:
            y, x = stack.pop()
            xs.append(x)
            ys.append(y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        blobs.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1, len(xs)))
    return blobs


def marker_candidates(img):
    """For each corner, the centres of solid dark squares that may be its square.

    They are looked for in the outer 40% of the page on both axes, nearest the
    corner first, so the page
    may be zoomed (about 55% up to where the squares leave the scan, ~125%),
    shifted or slightly tilted.
    """
    step = 4                                     # search on a quarter-size copy
    small = img[::step, ::step] < 100
    h, w = small.shape
    found = []
    for name in CORNERS:
        right, bottom = "right" in name, "bottom" in name
        x0, y0 = (int(w * 0.6) if right else 0), (int(h * 0.6) if bottom else 0)
        region = small[y0:y0 + int(h * 0.4) + 1, x0:x0 + int(w * 0.4) + 1]
        cands = []
        for bx0, by0, bx1, by1, n in dark_blobs(region):
            bw, bh = bx1 - bx0, by1 - by0
            size = (bw + bh) / 2 * step
            if not (0.5 * MARKER_SIZE <= size <= 1.6 * MARKER_SIZE):
                continue
            if not (0.75 <= bw / bh <= 1.33) or n < 0.8 * bw * bh:
                continue                         # not a solid square
            cx, cy = (x0 + (bx0 + bx1) / 2) * step, (y0 + (by0 + by1) / 2) * step
            dist = np.hypot(cx - (img.shape[1] if right else 0), cy - (img.shape[0] if bottom else 0))
            cands.append((dist, (x0 + bx0) * step, (y0 + by0) * step, (x0 + bx1) * step, (y0 + by1) * step))
        if not cands:
            raise ValueError(f"{name} corner square not found")
        centres = []
        for _, X0, Y0, X1, Y1 in sorted(cands)[:3]:
            # Centre at full resolution, from the dark pixels around the square.
            X0, Y0 = max(X0 - step, 0), max(Y0 - step, 0)
            ys, xs = np.nonzero(img[Y0:Y1 + step, X0:X1 + step] < 100)
            centres.append((X0 + xs.mean(), Y0 + ys.mean()))
        found.append(centres)
    return found


def fit_markers(img):
    """The affine map reference -> scan, and how far off its worst corner is.

    Of the candidate squares, the four that best form this form's rectangle win,
    so a blot or scanner shadow near a corner is passed over.
    """
    A = np.c_[REF_MARKERS, np.ones(4)]
    best = None
    for pts in itertools.product(*marker_candidates(img)):
        pts = np.array(pts)
        M, *_ = np.linalg.lstsq(A, pts, rcond=None)
        scale = np.sqrt(abs(np.linalg.det(M[:2])))
        err = np.abs(A @ M - pts).max() / scale
        if best is None or err < best[1]:
            best = (M, err, scale)
    return best


class Page:
    """A scan redrawn onto the reference page, so every position and size matches.

    The four corner squares fix how the scan is shifted, scaled and turned
    relative to the reference page; the scan is resampled accordingly.
    """

    def __init__(self, img):
        # An affine map puts three squares exactly; the fourth shows whether the
        # four really are this form's squares on a flat page.
        M, err, scale = fit_markers(img)                       # reference -> scan
        if err > 12:
            raise ValueError(f"corner squares do not form the expected rectangle (off by {err:.0f} px)")
        self.problems = []
        if err > 6:
            self.problems.append(f"corner squares slightly out of line ({err:.0f} px), page may be bent")
        if not 0.97 <= scale <= 1.03:
            self.problems.append(f"page scaled to {scale:.0%} of the reference")
        # PIL wants, for each output pixel, where to read in the scan.
        warped = Image.fromarray(img.astype(np.uint8)).transform(
            (PAGE_W, PAGE_H), Image.AFFINE,
            (M[0, 0], M[1, 0], M[2, 0], M[0, 1], M[1, 1], M[2, 1]),
            resample=Image.BICUBIC, fillcolor=255)
        self.img = np.asarray(warped, dtype=float)

    def to_page(self, x, y):
        return x, y                              # the page is already in the reference frame

    def crop(self, box, scale=1):
        x0, y0 = self.to_page(box[0], box[1])
        x1, y1 = self.to_page(box[2], box[3])
        im = Image.fromarray(self.img.astype(np.uint8)).crop(
            (int(x0), int(y0), int(x1), int(y1)))
        return im.resize((im.width * scale, im.height * scale)) if scale != 1 else im


# ---------------------------------------------------------------- bubbles

_yy, _xx = np.mgrid[-40:41, -40:41]
_rr = np.hypot(_xx, _yy)
_ang = (np.degrees(np.arctan2(_yy, _xx)) + 360) % 360
_bin = (_ang // (360 / NBINS)).astype(int)
INSIDE = _rr < R - 3
NEAR = (_rr >= R + 4) & (_rr <= 2 * R)          # where circles and X arms are drawn
FAR = (_rr > 2 * R) & (_rr <= 2.35 * R)         # X arms past a circle (the rings above and below start at 2.4 R)


def bin_ink(ink, ring):
    counts = np.bincount(_bin[ring & ink], minlength=NBINS)
    return counts >= BIN_INK_PIXELS


def runs(bins):
    """(centre, width) of each run of inked bins around the circle."""
    if bins.all():
        return [(0, NBINS)]
    start = int(np.argmin(bins))                 # begin on an empty bin
    out, i = [], 0
    while i < NBINS:
        if bins[(start + i) % NBINS]:
            j = i
            while j < NBINS and bins[(start + j) % NBINS]:
                j += 1
            out.append(((start + (i + j - 1) / 2) % NBINS, j - i))
            i = j
        else:
            i += 1
    return out


def strokes(bins):
    """('circle' | 'cross' | None) for the inked angular bins of one annulus."""
    if bins.mean() >= 0.7:
        return "circle"
    # A pen stroke through the bubble leaves two narrow arms on opposite sides;
    # ink smeared from a heavy fill spreads over a wider arc.
    arms = runs(bins)
    if any(w > 2 for _, w in arms):
        return None
    # Only arms leaning off the vertical and horizontal: the printed form has
    # small dashes straight above and below some bubbles.
    arms = [(c, w) for c, w in arms if abs(((c + 0.5) * 360 / NBINS - 45) % 90 - 45) >= 15]
    for i, (a, _) in enumerate(arms):
        for b, _ in arms[i + 1:]:
            if abs(abs(a - b) - NBINS / 2) <= 1.5:
                return "cross"
    return None


RING = np.abs(_rr - R) <= 1.5
PAPER = (_rr >= R + 3) & (_rr <= R + 6)        # paper just outside the printed ring
AROUND = (_rr >= R + 3) & (_rr <= 2.3 * R)


def locate(page, centres, search=6):
    """Page positions of a row of bubbles, nudged to where the printed rings are.

    Each bubble votes for the shift that puts a dark ring on light paper; the
    row moves by the median vote, so one messy bubble cannot drag it away.
    """
    pts = [tuple(int(round(v)) for v in page.to_page(cx, cy)) for cx, cy in centres]
    votes = []
    for x, y in pts:
        best = None
        for dy in range(-search, search + 1):
            for dx in range(-search, search + 1):
                v = ring_contrast(page, x + dx, y + dy)
                if best is None or v > best[0]:
                    best = (v, dx, dy)
        votes.append(best[1:])
    dx, dy = (int(round(np.median([v[i] for v in votes]))) for i in (0, 1))
    return [(x + dx, y + dy) for x, y in pts]


def measure(page, x, y, reach):
    """Fill and pen strokes of the bubble at (x, y).

    reach is how far out (in bubble radii) to look for strokes; printed box
    edges near the version and student-number bubbles would look like strokes.
    """
    patch = page.img[y - 40:y + 41, x - 40:x + 41]
    ink = patch < INK
    near = strokes(bin_ink(ink, NEAR & (_rr <= reach * R)))
    far = strokes(bin_ink(ink, FAR & (_rr <= reach * R))) if reach > 2 else None
    d = 255 - patch
    # The lightest paper around the bubble: row shading counts, a fill spilling
    # over the ring does not.
    paper = np.percentile(d[AROUND], 20)
    return {
        "fill": float(d[INSIDE].mean() - paper),
        "crossed": "cross" in (near, far),
        "circled": "circle" in (near, far),
        "xy": (x, y),
    }


def measure_row(page, centres, reach=2.35):
    return [measure(page, x, y, reach) for x, y in locate(page, centres)]


def grade(bubbles, base, level):
    """Label each bubble filled / faint / empty against the page's own levels."""
    full = max(base + 25, base + 0.45 * (level - base))
    for b in bubbles:
        f = b["fill"]
        b["state"] = "filled" if f >= full else "faint" if f >= base + 14 else "empty"
        # Strokes around a bubble with nothing in it change no answer.
        if b["state"] == "empty":
            b["crossed"] = b["circled"] = False


def describe(labels, bubbles):
    parts = []
    for lab, b in zip(labels, bubbles):
        tags = [b["state"]] if b["state"] != "empty" else []
        tags += [t for t in ("crossed", "circled") if b[t]]
        if tags:
            parts.append(f"{lab}:{'+'.join(tags)}")
    return " ".join(parts) or "no marks"


def decide(labels, bubbles):
    """Return (value, rule) where value is a label, '' for blank or None for review."""
    if any(b["crossed"] and b["circled"] for b in bubbles):
        return None, "a bubble is both crossed out and circled"
    live = [(l, b) for l, b in zip(labels, bubbles) if b["state"] != "empty" and not b["crossed"]]
    filled = [(l, b) for l, b in live if b["state"] == "filled"]
    faint = [(l, b) for l, b in live if b["state"] == "faint"]
    crossed = [l for l, b in zip(labels, bubbles) if b["crossed"]]
    circled_empty = [l for l, b in zip(labels, bubbles) if b["circled"] and b["state"] == "empty"]
    if circled_empty:
        return None, f"empty bubble circled ({''.join(circled_empty)})"
    if len(filled) > 1:
        return None, f"several bubbles filled ({''.join(l for l, _ in filled)})"
    if len(filled) == 1:
        if faint:
            return None, f"faint mark beside the fill ({''.join(l for l, _ in faint)})"
        lab, b = filled[0]
        if crossed:
            return lab, "others crossed out" + (", answer circled" if b["circled"] else "")
        return lab, "circled" if b["circled"] else "single fill"
    if len(faint) == 1:
        return faint[0][0], "single faint fill"
    if len(faint) > 1:
        return None, f"several faint marks ({''.join(l for l, _ in faint)})"
    if crossed:
        return "", "only crossed-out marks"
    return "", "no marks"


# ---------------------------------------------------------------- one sheet

def count_questions(page):
    """How many question rows are printed: the form leaves unused rows blank."""
    for q in range(MAX_Q):
        x, y = (int(round(v)) for v in page.to_page(*answer_centres(q)[0]))
        best = max(ring_contrast(page, x + dx, y + dy)
                   for dy in range(-4, 5) for dx in range(-4, 5))
        if best < 40:                           # printed rings score 60-110, paper 0
            return q
    return MAX_Q


def ring_contrast(page, x, y):
    p = 255 - page.img[y - 40:y + 41, x - 40:x + 41]
    return p[RING].mean() - p[PAPER].mean()


def read_sheet(page, nq, printed):
    """Read nq questions from a sheet on which `printed` question rows were found."""
    qs = [measure_row(page, answer_centres(q)) for q in range(min(nq, printed))]
    # The version and student-number bubbles sit near printed box edges, which
    # look like pen strokes further out, so only look close in for those.
    ver = measure_row(page, VERSION_CENTRES, reach=1.7)
    rows = [measure_row(page, [student_centres(c)[d] for c in range(STUDENT_COLS)], reach=2)
            for d in range(10)]
    stu = [[rows[d][c] for d in range(10)] for c in range(STUDENT_COLS)]

    everything = [b for row in qs for b in row] + ver + [b for col in stu for b in col]
    fills = np.array([b["fill"] for b in everything])
    base = float(np.median(fills))
    # How dark this student's marks are: the typical darkest bubble of rows that have a mark.
    row_max = np.array([max(b["fill"] for b in row) for row in qs + [ver] + stu])
    marked = row_max[row_max > base + 25]
    level = float(np.median(marked)) if len(marked) else base + 60
    grade(everything, base, level)

    fields = {f"Q{q + 1}": (row, LETTERS) for q, row in enumerate(qs)}
    fields["version"] = (ver, "ABCDE")
    digits = [decide("0123456789", b) + (describe("0123456789", b),) for b in stu]

    results = {}
    for name, (bubbles, labels) in fields.items():
        value, rule = decide(labels, bubbles)
        results[name] = (value, rule, describe(labels, bubbles))
    for q in range(len(qs), nq):
        results[f"Q{q + 1}"] = (None, "question row not found on this sheet", "")
    # A question may be left blank; the version may not.
    value, rule, marks = results["version"]
    if value == "":
        results["version"] = (None, f"no version marked ({rule})", marks)
    if all(d[0] for d in digits):
        # Columns settled by more than one clean fill are logged like answers.
        ruled = [(i, d) for i, d in enumerate(digits) if d[1] != "single fill"]
        rule = "; ".join(f"column {i + 1}: {d[1]}" for i, d in ruled) or "bubbles"
        marks = " | ".join(f"column {i + 1}: {d[2]}" for i, d in ruled)
        results["student"] = ("".join(d[0] for d in digits), rule, marks)
    else:
        bad = [f"column {i + 1}: {d[1]}" if d[0] is None else f"column {i + 1}: empty"
               for i, d in enumerate(digits) if not d[0]]
        results["student"] = (None, "; ".join(bad), " | ".join(d[2] for d in digits))
    return results, qs, ver, stu


def row_box(field):
    if field == "version":
        return VERSION_BOX
    if field == "student":
        return STUDENT_BOX
    cs = answer_centres(int(field[1:]) - 1)
    x0, y = cs[0]
    return (x0 - 70, y - 40, cs[-1][0] + 45, y + 40)


def debug_image(page, qs, ver, stu, path):
    im = Image.fromarray(page.img.astype(np.uint8)).convert("RGB")
    d = ImageDraw.Draw(im)
    colour = {"filled": "green", "faint": "orange", "empty": "#999"}
    for b in [b for row in qs for b in row] + ver + [b for col in stu for b in col]:
        x, y = b["xy"]
        d.ellipse((x - R, y - R, x + R, y + R), outline=colour[b["state"]], width=2)
        if b["crossed"]:
            d.line((x - R, y - R, x + R, y + R), fill="red", width=2)
            d.line((x - R, y + R, x + R, y - R), fill="red", width=2)
        if b["circled"]:
            d.ellipse((x - 2 * R, y - 2 * R, x + 2 * R, y + 2 * R), outline="blue", width=2)
    im.save(path)


# ---------------------------------------------------------------- overrides

def load_overrides(path):
    out = {}
    if path.exists():
        with path.open(newline="") as f:
            for row in csv.DictReader(r for r in f if r.strip() and not r.startswith("#")):
                value = (row.get("value") or "").strip().upper()
                out[(row["sheet"].strip(), row["field"].strip())] = "" if value in ("", "-") else value
    return out


def override_for(overrides, used, sheet, student, field):
    """The value overrides.csv gives this field, by sheet id or student number."""
    for key in (sheet, student):
        if key and (key, field) in overrides:
            used.add((key, field))
            return overrides[(key, field)]
    return None


# ---------------------------------------------------------------- main

def question_count(text):
    n = int(text)
    if not 1 <= n <= MAX_Q:
        raise argparse.ArgumentTypeError(f"must be 1 to {MAX_Q}")
    return n


def field_names(nq):
    return ["student", "version"] + [f"Q{q}" for q in range(1, nq + 1)]


def load_roster(path):
    """{student number: name} from the class list exported as tab-separated text.

    The list has a header line with a "Nº" column (and "Nome" for the name);
    every later line with digits in that column is a student.
    """
    roster, num_col, name_col = {}, None, None
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if num_col is None:
            heads = [c.lower() for c in cols]
            for key in ("nº", "n.º", "número", "numero", "number"):
                if key in heads:
                    num_col = heads.index(key)
                    name_col = next((heads.index(k) for k in ("nome", "name") if k in heads), None)
                    break
        elif len(cols) > num_col and cols[num_col].isdigit():
            roster[cols[num_col]] = cols[name_col] if name_col is not None and len(cols) > name_col else ""
    if num_col is None:
        raise SystemExit(f"{path}: no header line with a 'Nº' column")
    if not roster:
        raise SystemExit(f"{path}: no student numbers found")
    return roster


DECISION_COLS = ["sheet", "student", "field", "value", "decided_by", "reason", "marks"]
REVIEW_COLS = ["sheet", "student", "field", "problem", "marks", "image", "decision"]

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("inputs", nargs="+", help="PDF scans or page images")
    ap.add_argument("-o", "--out", help="output folder (default: out/ beside the first input)")
    ap.add_argument("--overrides", help="overrides CSV (default: overrides.csv beside the first input)")
    ap.add_argument("--roster", metavar="FILE",
                    help="class list (tab-separated, with a 'Nº' column) to check student numbers against")
    ap.add_argument("--questions", type=question_count, metavar="N",
                    help=f"number of questions, 1-{MAX_Q} (default: counted on the first sheet)")
    ap.add_argument("--debug", action="store_true", help="save each page with what was detected")
    args = ap.parse_args()
    roster = load_roster(args.roster) if args.roster else None

    home = Path(args.inputs[0]).resolve().parent
    out = Path(args.out) if args.out else home / "out"
    (out / "review").mkdir(parents=True, exist_ok=True)
    for old in (out / "review").glob("*.png"):
        old.unlink()
    ov_path = Path(args.overrides) if args.overrides else home / "overrides.csv"
    overrides = load_overrides(ov_path)
    used = set()

    nq, fields = args.questions, None
    answers, decisions, review = [], [], []
    many = len(args.inputs) > 1

    # First read every sheet, so student numbers can be compared across sheets.
    sheets = []
    with tempfile.TemporaryDirectory() as tmp:
        for src in args.inputs:
            for n, img in load_pages(src, tmp):
                sheet = f"{Path(src).stem}:p{n}" if many else f"p{n}"
                try:
                    page = Page(img)
                except ValueError as e:
                    print(f"{sheet}: skipped, {e}", file=sys.stderr)
                    review.append({"sheet": sheet, "field": "page", "problem": str(e), "decision": ""})
                    continue
                for p in page.problems:
                    print(f"{sheet}: warning, {p}", file=sys.stderr)
                printed = count_questions(page)
                if nq is None:
                    if not printed:
                        print(f"{sheet}: skipped, no question rows found", file=sys.stderr)
                        review.append({"sheet": sheet, "field": "page",
                                       "problem": "no question rows found", "decision": ""})
                        continue
                    nq = printed
                    print(f"{nq} questions on the form", file=sys.stderr)
                if fields is None:
                    fields = field_names(nq)
                if printed != nq:
                    print(f"{sheet}: warning, {printed} question rows found, reading {nq}",
                          file=sys.stderr)
                results, qs, ver, stu = read_sheet(page, nq, printed)
                if args.debug:
                    (out / "debug").mkdir(exist_ok=True)
                    debug_image(page, qs, ver, stu, out / "debug" / f"{sheet.replace(':', '_')}.png")
                # Pictures for anything that may need a person; the student number
                # always, as it may turn out to be on another sheet too.
                crops = {f: page.crop(row_box(f), scale=2) for f in fields
                         if f == "student" or results[f][0] is None}
                sheets.append((sheet, results, crops))

    # The same number on two sheets is a misread or a sheet scanned twice, and a
    # number missing from the class list is a misread or a student not enrolled:
    # a person decides, and the case stays listed so that decision can be changed.
    number = {}
    for sheet, results, _ in sheets:
        read = results["student"][0]
        ov = overrides.get((sheet, "student"))
        if ov is None and read:
            ov = overrides.get((read, "student"))
        number[sheet] = ov if ov is not None else read
    for sheet, results, _ in sheets:
        num = number[sheet]
        others = [s for s, n in number.items() if s != sheet and n and n == num]
        problems = [f"student number {num} is also on {', '.join(others)}"] if others else []
        if roster is not None and num and num not in roster:
            problems.append(f"student number {num} is not on the class list")
        if problems:
            _, rule, marks = results["student"]
            results["read_student"] = num
            results["student"] = (None, "; ".join(problems), marks)

    for sheet, results, crops in sheets:
        read = results.get("read_student") or results["student"][0]
        ov = override_for(overrides, used, sheet, read, "student")
        student = ov if ov is not None else results["student"][0]
        row = {"sheet": sheet, "student": student if student is not None else "?"}
        if roster is not None:
            row["name"] = roster.get(row["student"], "")
        pending = 0
        for field in fields:
            value, rule, marks = results[field]
            ov = override_for(overrides, used, sheet, read, field)
            case = {"sheet": sheet, "student": row["student"], "field": field, "marks": marks}
            if value is None:
                # Listed for review even once decided, so the decision can be changed.
                img_name = f"{sheet.replace(':', '_')}_{field}.png"
                crops[field].save(out / "review" / img_name)
                review.append({**case, "problem": rule, "image": f"review/{img_name}",
                               "decision": "" if ov is None else ov or "-"})
            if ov is not None:
                decisions.append({**case, "value": ov, "decided_by": "human", "reason": rule})
                value = ov
            elif value is None:
                pending += 1
            elif rule not in ("single fill", "no marks", "bubbles"):
                decisions.append({**case, "value": value, "decided_by": "rule", "reason": rule})
            if field != "student":
                row[field] = "?" if value is None else value
        row["needs_review"] = pending
        answers.append(row)
        print(f"{sheet}: student {row['student']}, {pending} to review", file=sys.stderr)

    def write(name, rows, cols):
        with (out / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    if fields is None:
        fields = field_names(nq or 0)
    write("answers.csv", answers,
          ["sheet", "student"] + (["name"] if roster is not None else []) + fields[1:] + ["needs_review"])
    # review.py looks names up here when a student number is decided on the page.
    if roster is not None:
        write("roster.csv", [{"student": k, "name": v} for k, v in roster.items()], ["student", "name"])
    else:
        (out / "roster.csv").unlink(missing_ok=True)
    write("decisions.csv", decisions, DECISION_COLS)
    write("review.csv", review, REVIEW_COLS)

    for sheet, field in overrides.keys() - used:
        print(f"warning: override {sheet},{field} matched no sheet", file=sys.stderr)
    if roster is not None:
        absent = sorted(set(roster) - {a["student"] for a in answers})
        print(f"{len(absent)} of {len(roster)} students on the class list have no sheet"
              + (": " + ", ".join(f"{k} {roster[k]}" for k in absent) if absent else ""),
              file=sys.stderr)
    pending = sum(1 for r in review if not r["decision"])
    print(f"{len(answers)} sheets, {len(decisions)} decisions logged, {pending} to review -> {out}/",
          file=sys.stderr)
    if pending:
        extra = "" if ov_path == out.parent / "overrides.csv" else f" --overrides {ov_path}"
        print(f"decide them with:  python3 {Path(__file__).parent / 'review.py'} {out}{extra}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
