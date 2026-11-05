# photo-to-pdf

[Русская версия](README.ru.md)

Turns phone photos of notebooks, books and paper documents into a clean PDF scan.
It splits two-page spreads at the real fold, corrects perspective and crops each
page tightly, without erasing faint pencil near the edges. It is an agent skill
for Claude Code and Codex (`SKILL.md`), and the same script works as a
standalone command-line tool.

The tool finds pages and folds automatically, flags placements that look
implausible, and renders material for checking every page against its photo. It
does not claim a fully automatic result: a person or an agent still looks at
every page before the PDF is final.

## What it does

- **Page detection.** Finds paper as a bright, low-saturation region, then snaps
  each edge to the paper-to-dark-background step and fits a robust straight line.
  Skin, a desk or a monitor next to the paper are not taken for paper.
- **Fold detection.** Finds the fold of a spread where the ruling lines break,
  not at the darkest point of the gutter shadow. On a thin notebook that is the
  thin crease line itself. In a deep shadow it is the foot of the brightness step
  on the shadow side.
- **Plausibility flags.** Marks off-centre or ambiguous folds, mismatched page
  pairs, pages touching the frame, aspect outliers, unruled halves (covers) and
  duplicates. A reviewer looks at those pages first.
- **One resampling.** Each page gets one perspective warp, with gentle lighting
  correction and no binarisation. JPEGs are embedded in the PDF without
  re-encoding. Optional crops change only PDF boxes; the image streams are
  verified unchanged.
- **Measurement and review.** Measures the physical page size from the grid
  cells. Renders every page, reports background or writing at page borders, and
  shows each page with a margin of the photo around its crop.

## Install

```bash
git clone https://github.com/Plasmoid77/photo-to-pdf ~/.claude/skills/photo-to-pdf   # Claude Code
# or ~/.codex/skills/photo-to-pdf for Codex
python3 -m pip install -r ~/.claude/skills/photo-to-pdf/requirements.txt             # or use system packages
```

Requirements: Python ≥ 3.10, Pillow, NumPy, OpenCV, ReportLab, pypdfium2 (for
`crop` and the tests), and Poppler `pdftoppm` (for `review` and for `measure` on
a PDF). If OpenCV comes from system packages, create the virtual environment on
the same interpreter with `python3 -m venv --system-site-packages`.

## Workflow

```bash
S=~/.claude/skills/photo-to-pdf/scripts/scan.py
python3 $S prepare photos/ --work work --layout auto          # draft manifest + contact sheets
python3 $S inspect work/manifest.json --work work/inspect --ids 0006-L,0006-R   # corner close-ups for manual fixes
# check every outline, fix quads, exclusions and order, then set "reviewed": true
python3 $S build work/manifest.json --output work/draft.pdf   # one warp per page
python3 $S measure work/draft-pages --work work/measure       # page size from 5 mm cells -> aspect, --width-mm
python3 $S build work/manifest.json --output work/final.pdf --width-mm 165
python3 $S review work/final.pdf --work work/review          # renders, contact sheets, edge report
python3 $S compare work/final.scan.json --work work/compare   # what lies outside each crop
```

`crop` trims finished PDF pages without re-encoding them; `eval` compares a
fresh `prepare` with a verified manifest of the same photos (see below). Every
command, the manifest format and the flags are described in
[references/commands.md](references/commands.md) (in Russian). `scan.py <command> -h`
lists the options.

## Accuracy

Checked on two real notebooks (photos not included):

| | photos | pages | fold error, median | fold error, worst unflagged | silent failures (> 3 % of page width) |
|---|---|---|---|---|---|
| thick notebook, deep gutter shadow, desk and monitor around | 78 | 155 | 3.2 px | 13.5 px | 0 |
| thin school notebook held in the hand | 48 | 95 | 2.4 px | 9.7 px | 0 |

The fold reference is the ruling break, marked by eye at three heights on each of
122 spreads. The earlier shadow-edge method put the fold a median 7 px into the
right-hand page of the thick notebook. 10–13 % of pages were flagged; these
were mostly legitimate (cover spreads, pages cut by the frame, a monitor merged
with the paper).

Known limits: a page edge is fitted with a straight line, so a strongly curled
page keeps a sliver of background at the corners or loses a few pixels of paper.
Writing that is outside the photo cannot be recovered.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests generate synthetic spreads with and without a gutter shadow, plus a
cardboard cover. They check detection accuracy and flags, `eval` (including a
detected failure), `build` page size, refusal to build unreviewed pages, `crop`
with unchanged image streams, `measure` of a known grid, `compare`, `inspect` and
`review`.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
