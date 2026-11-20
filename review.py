#!/usr/bin/env python3
"""A local page for deciding the answers mcq2csv.py left for a person.

Shows every case in OUTDIR/review.csv with its picture and the possible
answers as toggles.  Each click is saved at once:

- overrides.csv gets the decision, so rerunning mcq2csv.py keeps it;
- answers.csv gets the answer in place of "?";
- decisions.csv gets a line with the problem, the choice and decided_by=human;
- review.csv records the decision, so the page shows it next time.

Clicking the selected toggle again takes the decision back.

Usage:  python3 review.py [OUTDIR] [--overrides FILE] [--port 8765] [--no-browser]
        (on Windows, type  py  instead of  python3)
"""
import argparse
import csv
import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mcq2csv import CSV_ENCODING, DECISION_COLS, REVIEW_COLS

NO_ANSWER = "-"


def options(field):
    if field == "version":
        return list("ABCDE")
    if re.fullmatch(r"Q\d+", field):
        return list("ABCD")
    return None                                 # student number: typed in


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding=CSV_ENCODING) as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, cols):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding=CSV_ENCODING) as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


class Store:
    def __init__(self, out, overrides):
        self.out, self.ov_path = out, overrides
        self.review_path = out / "review.csv"
        if not self.review_path.exists():
            sys.exit(f"{self.review_path} not found: run mcq2csv.py first")

    def cases(self):
        rows = read_csv(self.review_path)
        for r in rows:
            r["options"] = options(r["field"])
        return rows

    def decide(self, sheet, field, value):
        """Record value ('' takes the decision back) and return the updated case."""
        value = value.strip().upper()
        opts = options(field)
        if value and value != NO_ANSWER:
            if opts is not None and value not in opts:
                raise ValueError(f"{value!r} is not an option for {field}")
            if opts is None and not re.fullmatch(r"\d{5}", value):
                raise ValueError("the student number has 5 digits")

        self.check_writable()
        review = read_csv(self.review_path)
        case = next((r for r in review if r["sheet"] == sheet and r["field"] == field), None)
        if case is None:
            raise KeyError(f"no review case {sheet} {field}")
        case["decision"] = value

        answers = read_csv(self.out / "answers.csv")
        with (self.out / "answers.csv").open(newline="", encoding=CSV_ENCODING) as f:
            answer_cols = next(csv.reader(f))         # the question count varies by form
        row = next(r for r in answers if r["sheet"] == sheet)
        old_student = row["student"]
        cell = "?" if not value else "" if value == NO_ANSWER else value
        row[field] = cell
        row["needs_review"] = sum(1 for r in review if r["sheet"] == sheet and not r["decision"])
        student = row["student"]
        roster = {r["student"]: r["name"] for r in read_csv(self.out / "roster.csv")}
        if "name" in row:
            row["name"] = roster.get(student, "")

        decisions = [d for d in read_csv(self.out / "decisions.csv")
                     if not (d["sheet"] == sheet and d["field"] == field)]
        if value:
            decisions.append({"sheet": sheet, "student": student, "field": field,
                              "value": "" if value == NO_ANSWER else value,
                              "decided_by": "human", "reason": case["problem"],
                              "marks": case["marks"]})
        if student != old_student:
            for r in review + decisions:
                if r["sheet"] == sheet:
                    r["student"] = student

        self.save_override(sheet, old_student, field, value)
        write_csv(self.out / "answers.csv", answers, answer_cols)
        write_csv(self.out / "decisions.csv", decisions, DECISION_COLS)
        write_csv(self.review_path, review, REVIEW_COLS)
        case["options"] = opts
        case["note"] = ""
        if field == "student" and value and roster:
            case["note"] = (roster[value] if value in roster
                            else "Saved, but this number is not on the class list.")
        return case

    def check_writable(self):
        """Fail before writing anything if a file is locked, so no click is half saved.

        On Windows a CSV file open in Excel cannot be written.
        """
        for path in (self.out / "answers.csv", self.out / "decisions.csv", self.review_path,
                     self.ov_path):
            if path.exists():
                try:
                    with path.open("a"):
                        pass
                except PermissionError:
                    raise PermissionError(f"{path.name} is open in another program (Excel?). "
                                          "Close it and click again.") from None

    def save_override(self, sheet, student, field, value):
        """Replace this field's line in overrides.csv, keeping comments and other lines."""
        lines = []
        if self.ov_path.exists():
            with self.ov_path.open(newline="", encoding=CSV_ENCODING) as f:
                lines = list(csv.reader(f))
        if not lines:
            lines = [["sheet", "field", "value", "note"]]
        keys = {sheet, student} - {"", "?"}
        kept = [lines[0]] + [l for l in lines[1:]
                             if not (len(l) >= 2 and l[0].strip() in keys and l[1].strip() == field)]
        if value:
            kept.append([sheet, field, value, "review page"])
        tmp = self.ov_path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding=CSV_ENCODING) as f:
            csv.writer(f).writerows(kept)
        tmp.replace(self.ov_path)


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                self.send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/cases":
                self.send(200, json.dumps({"cases": store.cases(), "overrides": str(store.ov_path)}))
            elif path.startswith("/review/"):
                img = store.out / "review" / Path(path).name
                if img.suffix == ".png" and img.is_file():
                    self.send(200, img.read_bytes(), "image/png")
                else:
                    self.send(404, "not found", "text/plain")
            else:
                self.send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/decide":
                return self.send(404, "not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                case = store.decide(body["sheet"], body["field"], body.get("value", ""))
                self.send(200, json.dumps(case))
            except (ValueError, KeyError) as e:
                self.send(400, json.dumps({"error": e.args[0]}))
            except PermissionError as e:
                self.send(409, json.dumps({"error": str(e)}))

        def log_message(self, *args):
            pass

    return Handler


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCQ review</title>
<style>
:root {
  --bg: #f6f6f4; --card: #fff; --text: #1d1d1b; --muted: #6b6b66; --line: #deddd8;
  --accent: #2f5fd0; --accent-text: #fff; --ok: #1f7a4a; --warn: #a3560d; --err: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --card: #20201e; --text: #ecebe7; --muted: #9b9a94; --line: #3a3936;
    --accent: #7ea2ff; --accent-text: #0d1430; --ok: #5cc48a; --warn: #e0a05a; --err: #ff8a80;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { position: sticky; top: 0; z-index: 1; background: var(--bg);
         border-bottom: 1px solid var(--line); padding: 12px 16px;
         display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center; }
h1 { font-size: 18px; margin: 0; }
#progress { color: var(--muted); }
label.filter { margin-left: auto; color: var(--muted); display: flex; gap: 6px; align-items: center; }
main { max-width: 760px; margin: 0 auto; padding: 16px; display: grid; gap: 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
.card.done { border-color: color-mix(in srgb, var(--ok) 45%, var(--line)); }
.head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.title { font-weight: 600; font-size: 16px; }
.state { font-size: 13px; color: var(--warn); }
.card.done .state { color: var(--ok); }
.problem { margin: 4px 0 0; }
.marks { margin: 2px 0 12px; color: var(--muted); font: 12px ui-monospace, Menlo, monospace; }
.pic { background: #fff; border-radius: 6px; padding: 4px; border: 1px solid var(--line); }
.pic img { display: block; width: 100%; height: auto; }
.choices { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
button.opt { min-width: 52px; padding: 10px 14px; font: 600 16px system-ui, sans-serif;
             border: 1px solid var(--line); border-radius: 8px; background: var(--card);
             color: var(--text); cursor: pointer; }
button.opt:hover { border-color: var(--accent); }
button.opt[aria-pressed="true"] { background: var(--accent); color: var(--accent-text);
                                  border-color: var(--accent); }
button.opt.none { font-weight: 500; }
input.num { font: 600 18px ui-monospace, Menlo, monospace; letter-spacing: 3px; width: 9ch;
            padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px;
            background: var(--card); color: var(--text); }
.msg { font-size: 13px; margin-top: 8px; min-height: 1.2em; color: var(--muted); }
.msg.err { color: var(--err); }
.empty { text-align: center; color: var(--muted); padding: 40px 0; }
footer { max-width: 760px; margin: 0 auto; padding: 0 16px 32px; color: var(--muted); font-size: 13px; }
code { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>MCQ review</h1>
  <span id="progress"></span>
  <label class="filter"><input type="checkbox" id="only"> Only undecided</label>
</header>
<main id="list"></main>
<footer>Each choice is saved straight away to answers.csv, decisions.csv and
<code id="ovpath">overrides.csv</code>. Click a selected choice again to undo it.</footer>
<script>
let cases = [];
const list = document.getElementById("list");
const only = document.getElementById("only");
try { only.checked = localStorage.getItem("only") === "1"; } catch (e) {}
only.addEventListener("change", () => {
  try { localStorage.setItem("only", only.checked ? "1" : "0"); } catch (e) {}
  render();
});

function label(v) { return v === "-" ? "No answer" : v; }

function progress() {
  const done = cases.filter(c => c.decision).length;
  document.getElementById("progress").textContent =
    `${done} of ${cases.length} decided` + (done === cases.length && cases.length ? " ✓" : "");
}

function card(c) {
  const el = document.createElement("section");
  el.className = "card" + (c.decision ? " done" : "");
  const who = c.student && c.student !== "?" ? ` · student ${c.student}` : "";
  el.innerHTML = `
    <div class="head">
      <span class="title"></span>
      <span class="state"></span>
    </div>
    <p class="problem"></p>
    <p class="marks"></p>
    ${c.image ? `<div class="pic"><img alt=""></div>` : ""}
    <div class="choices"></div>
    <div class="msg" aria-live="polite"></div>`;
  el.querySelector(".title").textContent = `${c.sheet}${who} · ${c.field}`;
  el.querySelector(".state").textContent = c.decision ? `Decided: ${label(c.decision)}` : "Undecided";
  el.querySelector(".problem").textContent = c.problem;
  el.querySelector(".marks").textContent = c.marks ? `Detected: ${c.marks}` : "";
  if (c.image) {
    const img = el.querySelector("img");
    img.src = c.image;
    img.alt = `Scan of ${c.field} on ${c.sheet}`;
  }
  const box = el.querySelector(".choices");
  const msg = el.querySelector(".msg");
  msg.textContent = c.note || "";
  if (c.options) {
    for (const v of [...c.options, "-"]) {
      const b = document.createElement("button");
      b.className = "opt" + (v === "-" ? " none" : "");
      b.textContent = label(v);
      b.setAttribute("aria-pressed", c.decision === v ? "true" : "false");
      b.addEventListener("click", () => decide(c, c.decision === v ? "" : v, msg));
      box.appendChild(b);
    }
  } else if (c.field === "student") {
    const inp = document.createElement("input");
    inp.className = "num"; inp.inputMode = "numeric"; inp.maxLength = 5;
    inp.placeholder = "00000"; inp.value = c.decision || "";
    inp.setAttribute("aria-label", "Student number");
    const save = document.createElement("button");
    save.className = "opt"; save.textContent = "Save";
    save.addEventListener("click", () => decide(c, inp.value, msg));
    inp.addEventListener("keydown", e => { if (e.key === "Enter") decide(c, inp.value, msg); });
    box.append(inp, save);
    if (c.decision) {
      const undo = document.createElement("button");
      undo.className = "opt none"; undo.textContent = "Undo";
      undo.addEventListener("click", () => decide(c, "", msg));
      box.appendChild(undo);
    }
  } else {
    msg.textContent = "This page could not be read; rescan it.";
  }
  return el;
}

function render() {
  list.replaceChildren();
  const shown = cases.filter(c => !only.checked || !c.decision);
  if (!shown.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = cases.length ? "Everything is decided." : "Nothing to review.";
    list.appendChild(p);
  }
  for (const c of shown) list.appendChild(card(c));
  progress();
}

async function decide(c, value, msg) {
  msg.className = "msg"; msg.textContent = "Saving…";
  try {
    const r = await fetch("/api/decide", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({sheet: c.sheet, field: c.field, value})
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    Object.assign(c, data);
    const y = window.scrollY;
    render();
    window.scrollTo(0, y);
  } catch (e) {
    msg.className = "msg err"; msg.textContent = "Not saved: " + e.message;
  }
}

fetch("/api/cases").then(r => r.json()).then(d => {
  cases = d.cases;
  document.getElementById("ovpath").textContent = d.overrides;
  render();
}).catch(e => { list.textContent = "Could not load the cases: " + e.message; });
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", nargs="?", default=str(Path(__file__).resolve().parent / "out"),
                    help="mcq2csv.py output folder (default: out/ beside this script)")
    ap.add_argument("--overrides", help="overrides CSV (default: overrides.csv beside OUTDIR)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    store = Store(out, Path(args.overrides) if args.overrides else out.parent / "overrides.csv")
    server = HTTPServer(("127.0.0.1", args.port), make_handler(store))
    url = f"http://localhost:{args.port}/"
    print(f"Review page: {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
