"""End-to-end tests of scripts/scan.py on synthetic photographs (no personal data).

Run: python3 -m unittest discover -s tests -v   (needs Pillow, NumPy, OpenCV, ReportLab, pypdfium2;
review tests also need Poppler pdftoppm and are skipped without it).
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

SCAN = Path(__file__).resolve().parents[1] / 'scripts' / 'scan.py'
PAGE_W, PAGE_H = 1300, 1650          # flat page size in px (about 8 px/mm for a 165 x 210 mm page)
CELL = 40                            # 5 mm grid cell in px


def run(*args, check=True):
    result = subprocess.run([sys.executable, str(SCAN), *map(str, args)], capture_output=True, text=True)
    if check and result.returncode:
        raise AssertionError(f'scan.py {args[0]} failed:\n{result.stdout}\n{result.stderr}')
    return result


def page_texture(rng, ruled=True, ink=True, colour=(236, 233, 224)):
    page = np.zeros((PAGE_H, PAGE_W, 3), np.uint8)
    page[:] = colour
    if ruled:
        for y in range(CELL // 2, PAGE_H, CELL):
            page[y:y + 2] = (170, 185, 200)
        for x in range(CELL // 2, PAGE_W, CELL):
            page[:, x:x + 2] = (170, 185, 200)
    if ink:
        for _ in range(40):
            x, y = rng.integers(60, PAGE_W - 200), rng.integers(60, PAGE_H - 60)
            points = np.cumsum(rng.integers(-12, 16, (12, 2)), axis=0) + [x, y]
            cv2.polylines(page, [points.astype(np.int32)], False, (150, 60, 40), 3)
    return page


def spread_photo(rng, shadow=False, crease=True, cover_right=False, background=(35, 30, 28)):
    """A two-page spread warped into a 3200 x 2200 photo. Returns RGB image and true quads (L, R)."""
    flat = np.concatenate([page_texture(rng),
                           page_texture(rng, ruled=False, ink=False, colour=(205, 195, 175)) if cover_right
                           else page_texture(rng)], axis=1)
    if shadow:
        x = np.arange(2 * PAGE_W, dtype=float)
        dark = np.where(x < PAGE_W, np.exp(-(PAGE_W - x) / 45.) * 70, np.exp(-(x - PAGE_W) / 8.) * 25)
        flat = np.clip(flat - dark[None, :, None], 0, 255).astype(np.uint8)
    if crease:
        flat[:, PAGE_W - 1:PAGE_W + 1] = (120, 110, 100)
    photo = np.zeros((2200, 3200, 3), np.uint8)
    photo[:] = background
    corners = np.float32([[0, 0], [2 * PAGE_W, 0], [2 * PAGE_W, PAGE_H], [0, PAGE_H]])
    target = np.float32([[420, 260], [2900, 300], [2960, 1960], [360, 1900]])
    matrix = cv2.getPerspectiveTransform(corners, target)
    cv2.warpPerspective(flat, matrix, (3200, 2200), photo, borderMode=cv2.BORDER_TRANSPARENT,
                        flags=cv2.INTER_LINEAR)
    def project(points):
        return cv2.perspectiveTransform(np.float32(points).reshape(-1, 1, 2), matrix)[:, 0] / [3200, 2200]
    left = project([[0, 0], [PAGE_W, 0], [PAGE_W, PAGE_H], [0, PAGE_H]])
    right = project([[PAGE_W, 0], [2 * PAGE_W, 0], [2 * PAGE_W, PAGE_H], [PAGE_W, PAGE_H]])
    return photo, left, right


def corner_error(a, b, width_px):
    """Largest corner distance as % of the page width."""
    return float(np.linalg.norm((np.float64(a) - np.float64(b)) * [3200, 2200], axis=1).max() / width_px * 100)


class Detection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='photo-to-pdf-test-'))
        self.photos = self.tmp / 'photos'
        self.photos.mkdir()
        self.rng = np.random.default_rng(7)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def save(self, name, image):
        Image.fromarray(image).save(self.photos / name, quality=93)

    def prepare(self):
        run('prepare', self.photos, '--work', self.tmp / 'work')
        return json.loads((self.tmp / 'work' / 'manifest.json').read_text())

    def test_spreads_with_and_without_shadow(self):
        truth = {}
        for name, shadow in [('01-shadow.jpg', True), ('02-thin.jpg', False)]:
            image, left, right = spread_photo(self.rng, shadow=shadow)
            self.save(name, image)
            truth[name] = (left, right)
        manifest = self.prepare()
        pages = {(Path(p['source']).name, p['id'][-1]): p for p in manifest['pages']}
        self.assertEqual(len(manifest['pages']), 4)
        width = np.linalg.norm((truth['01-shadow.jpg'][0][1] - truth['01-shadow.jpg'][0][0]) * [3200, 2200])
        for name, (left, right) in truth.items():
            for side, quad in (('L', left), ('R', right)):
                page = pages[(name, side)]
                self.assertEqual(page['detection'], 'edges+gutter')
                self.assertEqual(page['flags'], [], f'{name} {side} unexpectedly flagged')
                self.assertLess(corner_error(page['quad'], quad, width), 1.0, f'{name} {side}')

    def test_cover_side_is_flagged(self):
        # unruled cardboard (inside of a cover) next to a page: both are paper-coloured
        image, _, _ = spread_photo(self.rng, cover_right=True)
        self.save('01-cover.jpg', image)
        manifest = self.prepare()
        flags = {f for p in manifest['pages'] for f in p['flags']}
        self.assertIn('one-side-unruled', flags)

    def test_eval_against_reference(self):
        image, left, right = spread_photo(self.rng, shadow=True)
        self.save('01.jpg', image)
        manifest = self.prepare()
        reference = json.loads(json.dumps(manifest))
        for page in reference['pages']:
            page['quad'] = (left if page['id'].endswith('-L') else right).tolist()
        (self.tmp / 'ref.json').write_text(json.dumps(reference))
        good = run('eval', self.tmp / 'work' / 'manifest.json', '--reference', self.tmp / 'ref.json')
        self.assertIn('EVAL OK', good.stdout)
        for page in reference['pages']:
            page['quad'][1][0] += .05          # move a corner by about 6 % of the page width
        (self.tmp / 'bad.json').write_text(json.dumps(reference))
        bad = run('eval', self.tmp / 'work' / 'manifest.json', '--reference', self.tmp / 'bad.json', check=False)
        self.assertEqual(bad.returncode, 1)
        self.assertIn('SILENT', bad.stdout)


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='photo-to-pdf-test-'))
        photos = cls.tmp / 'photos'
        photos.mkdir()
        image, _, _ = spread_photo(np.random.default_rng(3), shadow=True)
        Image.fromarray(image).save(photos / 'spread.jpg', quality=93)
        run('prepare', photos, '--work', cls.tmp / 'work')
        manifest = json.loads((cls.tmp / 'work' / 'manifest.json').read_text())
        for page in manifest['pages']:
            page['reviewed'] = True
        cls.manifest = cls.tmp / 'work' / 'manifest.json'
        cls.manifest.write_text(json.dumps(manifest))
        cls.pdf = cls.tmp / 'out' / 'draft.pdf'
        run('build', cls.manifest, '--output', cls.pdf, '--width-mm', 165, '--quality', 90)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_build_page_size_and_record(self):
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(self.pdf)
        self.assertEqual(len(document), 2)
        for page in document:
            self.assertAlmostEqual(page.get_width() * 25.4 / 72, 165, places=1)
        record = json.loads(self.pdf.with_suffix('.scan.json').read_text())
        self.assertEqual([p['page'] for p in record['pages']], [1, 2])

    def test_unreviewed_pages_refuse_to_build(self):
        manifest = json.loads(self.manifest.read_text())
        manifest['pages'][0]['reviewed'] = False
        path = self.tmp / 'unreviewed.json'
        path.write_text(json.dumps(manifest))
        result = run('build', path, '--output', self.tmp / 'x.pdf', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reviewed', result.stderr)

    def test_crop_keeps_image_streams(self):
        plan = self.tmp / 'plan.json'
        plan.write_text(json.dumps({'1': [.02, .01, .98, .99]}))
        result = run('crop', self.pdf, '--plan', plan, '--output', self.tmp / 'cropped.pdf')
        self.assertIn('image streams unchanged', result.stdout)

    def test_measure_recovers_grid(self):
        result = run('measure', self.pdf.parent / 'draft-pages', '--work', self.tmp / 'measure')
        summary = json.loads((self.tmp / 'measure' / 'measure.json').read_text())['summary']
        self.assertEqual(summary['grid_pages'], 2, result.stdout)
        # synthetic page: 1300 x 1650 px at 40 px per 5 mm cell = 162.5 x 206.25 mm
        self.assertAlmostEqual(summary['width_mm'], 162.5, delta=162.5 * .015)
        self.assertAlmostEqual(summary['aspect'], 1300 / 1650, delta=.012)

    def test_compare_writes_every_page(self):
        run('compare', self.pdf.with_suffix('.scan.json'), '--work', self.tmp / 'compare')
        self.assertEqual(len(list((self.tmp / 'compare').glob('compare-*.jpg'))), 2)

    def test_inspect_writes_sheet(self):
        run('inspect', self.manifest, '--work', self.tmp / 'inspect')
        self.assertEqual(len(list((self.tmp / 'inspect').glob('inspect-*.jpg'))), 1)

    @unittest.skipUnless(shutil.which('pdftoppm'), 'Poppler pdftoppm not installed')
    def test_review_reports_edges(self):
        result = run('review', self.pdf, '--work', self.tmp / 'review')
        rows = json.loads((self.tmp / 'review' / 'edges.json').read_text())
        self.assertEqual(len(rows), 2)
        self.assertIn('pages rendered', result.stdout)


if __name__ == '__main__':
    unittest.main()
