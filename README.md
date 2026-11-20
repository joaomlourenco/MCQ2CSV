# MCQ2CSV

Reads scanned Akindi answer sheets (student number, version A–E, 1 to 50
questions A–D) into a CSV.

## Run

```bash
python3 mcq2csv.py scans.pdf
```

Needs Python 3 with `numpy` and `Pillow`, and poppler's `pdftoppm` for PDFs
(`brew install poppler`). Several PDFs or page images can be given at once.

Options:

- `--roster inscritos.txt` checks every student number against the class list
  (tab-separated text with a header line containing a `Nº` column, and `Nome` for
  the name). A number not on the list goes to review, `answers.csv` gets a `name`
  column, and the run ends by listing the students on the list with no sheet.
- `--questions N` (1–50) sets the number of questions. Without it, the script
  counts the printed question rows on the first sheet (the form leaves unused rows
  blank) and warns about any sheet where it finds a different number; questions
  missing from a sheet go to review.

Output goes to `out/` beside the first input:

| File | Contents |
|---|---|
| `answers.csv` | One row per sheet: `sheet, student, [name,] version, Q1…QN, needs_review`. Blank = no answer, `?` = waiting for your decision. |
| `decisions.csv` | Every answer not settled by "one clean fill": the value, `decided_by` (`rule` or `human`), the reason or problem, and the marks detected. |
| `review.csv`, `review/` | What needs a person, with a picture of each row and the decision once made. |

`--debug` also saves every page to `out/debug/` with what was detected drawn on it
(green = filled, orange = faint, red X = crossed out, blue ring = circled).

## How marks are decided

Decided by the script (and logged in `decisions.csv` unless it was a single clean fill):

- one filled bubble → that answer, even if it is circled
- one filled bubble, the others crossed out → that answer
- only crossed-out marks, or nothing → no answer
- one faint fill and nothing else → that answer

Sent to review:

- two or more filled bubbles that are not crossed out
- a bubble that is both crossed out and circled, or an empty bubble that is circled
- a faint mark next to a filled one (often an erased answer)
- a student-number column without exactly one digit, or no version marked
- a student number that is also on another sheet (a misread, or a sheet scanned
  twice), or with `--roster`, one that is not on the class list; once you
  confirm it, it stays listed on the review page as decided
- a question row the script cannot find on a sheet

The student-number columns follow the same rules as the answers, one column at a
time: a crossed-out digit is ignored, and the column is logged in `decisions.csv`.

## Your decisions: the review page

```bash
python3 review.py
```

This opens http://localhost:8765/ with every case from `out/review.csv`: the picture of the
row, what was detected, and the answers as toggles (plus "No answer"; a typed
5-digit number for a student number; with `--roster`, the page shows the
student's name, or warns if the number is not on the list). Each click is saved
immediately:

- `overrides.csv` gets the decision, so rerunning `mcq2csv.py` keeps it;
- `answers.csv` gets the answer in place of `?`;
- `decisions.csv` gets a line with the problem, the choice and `decided_by=human`;
- `review.csv` records it, so the page shows it the next time.

Click a selected toggle again to undo it. Stop the page with Ctrl-C. For another
output folder: `python3 review.py OUTDIR [--overrides FILE] [--port N]`.

## Your decisions: `overrides.csv`

The review page writes this file for you; you can also edit it by hand. It lives
beside the PDF (or pass `--overrides FILE`) and is applied on every run:

```csv
sheet,field,value,note
p5,Q1,C,circled C; A and D crossed out
12345,Q4,-,can't tell
p10,version,B,
```

- `sheet` is the sheet id from `answers.csv` (`p5`) or the student number.
- `field` is `Q1`…`Q50`, `version` or `student`.
- `value` is the answer; `-` or empty means no answer.

Overrides win over what the script reads, so rerunning never asks the same
question twice. A line that matches no sheet is reported as a warning.

## Form layout

Positions are measured on this form at 200 dpi and kept as constants at the top
of `mcq2csv.py`. Questions 1–25 fill the first answer column and 26–50 the second;
rows 31–50 assume the same spacing as the rows measured on this 30-question form.
Every row is nudged onto its printed bubbles before it is read. A different form
layout needs those constants measured again.

Scans need not match that size. The script finds the four black corner squares
anywhere near the corners, whatever their size, and redraws the page onto the
reference page before reading it. Pages zoomed from about 55% up to where the
squares leave the scan (~125%), shifted, slightly tilted or scanned at another
resolution read the same; a zoom of more than 3% is reported as a warning. A blot
near a corner is passed over: of the candidate squares, the four that best form
the form's rectangle are used. A page with a square cut off or covered cannot be
aligned: it is skipped and listed in `review.csv` to be rescanned.
