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


def spread_photo(rng, shadow=False, crease=True, cover_right=False, background=(35, 30, 28), bulge=0,
                 date=False, dip=0, ruled=True):
    """A two-page spread warped into a 3200 x 2200 photo. Returns RGB image and true quads (L, R).

    `bulge` curves the outer edge of the left page outward by up to that many px (mid-height);
    `date` writes a blue mark in that bulge, outside the straight page rectangle.
    `dip` bends the top edge of both pages down towards the fold, by that many px at the fold.
    """
    flat = np.concatenate([page_texture(rng, ruled),
                           page_texture(rng, ruled=False, ink=False, colour=(205, 195, 175)) if cover_right
                           else page_texture(rng, ruled)], axis=1)
    if bulge:
        extra = np.zeros((PAGE_H, bulge, 3), np.uint8)
        extra[:] = background
        for y in range(PAGE_H):
            extra[y, bulge - int(round(bulge * np.sin(np.pi * y / PAGE_H))):] = (236, 233, 224)
        if date:
            for x in range(bulge - 50, bulge - 8, 12):
                cv2.line(extra, (x, PAGE_H // 2 - 40), (x + 6, PAGE_H // 2 + 40), (40, 60, 150), 3)
        flat = np.concatenate([extra, flat], axis=1)
    if shadow:
        x = np.arange(2 * PAGE_W, dtype=float)
        dark = np.where(x < PAGE_W, np.exp(-(PAGE_W - x) / 45.) * 70, np.exp(-(x - PAGE_W) / 8.) * 25)
        flat = np.clip(flat - dark[None, :, None], 0, 255).astype(np.uint8)
    if dip:
        x = np.arange(2 * PAGE_W, dtype=float)
        drop = dip * np.clip(1 - np.abs(x - PAGE_W) / (.1 * PAGE_W), 0, 1) ** 2
        flat[np.arange(PAGE_H)[:, None] < drop[None, :]] = background
    if crease:
        flat[:, bulge + PAGE_W - 1:bulge + PAGE_W + 1] = (120, 110, 100)
    photo = np.zeros((2200, 3200, 3), np.uint8)
    photo[:] = background
    corners = np.float32([[0, 0], [2 * PAGE_W, 0], [2 * PAGE_W, PAGE_H], [0, PAGE_H]])
    target = np.float32([[420, 260], [2900, 300], [2960, 1960], [360, 1900]])
    matrix = cv2.getPerspectiveTransform(corners, target)
    shift = np.array([[1, 0, -bulge], [0, 1, 0], [0, 0, 1]], float)
    cv2.warpPerspective(flat, matrix @ shift, (3200, 2200), photo, borderMode=cv2.BORDER_TRANSPARENT,
                        flags=cv2.INTER_LINEAR)
    def project(points):
        return cv2.perspectiveTransform(np.float32(points).reshape(-1, 1, 2), matrix)[:, 0] / [3200, 2200]
    left = project([[0, 0], [PAGE_W, 0], [PAGE_W, PAGE_H], [0, PAGE_H]])
    right = project([[PAGE_W, 0], [2 * PAGE_W, 0], [2 * PAGE_W, PAGE_H], [PAGE_W, PAGE_H]])
    return photo, left, right


def landscape_photo(kind):
    """Loose landscape sheet with printing that must not count as a fold."""
    sheet = np.full((1650, 2334, 3), (236, 233, 224), np.uint8)
    if kind == 'text':
        for y in range(180, 1500, 80):
            cv2.putText(sheet, 'Printed text on a single landscape sheet', (140, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.8, (40, 40, 40), 3)
    elif kind == 'table':
        for y in range(180, 1501, 110):
            cv2.line(sheet, (180, y), (2154, y), (40, 40, 40), 3)
        for x in (180, 1167, 2154):
            cv2.line(sheet, (x, 180), (x, 1500), (40, 40, 40), 3)
    elif kind == 'grid':
        for y in range(20, 1650, 40):
            cv2.line(sheet, (0, y), (2333, y), (170, 185, 200), 2)
        for x in range(20, 2334, 40):
            cv2.line(sheet, (x, 0), (x, 1649), (170, 185, 200), 2)
        cv2.line(sheet, (1167, 0), (1167, 1649), (200, 70, 70), 3)
    photo = np.full((2200, 3200, 3), (35, 30, 28), np.uint8)
    matrix = cv2.getPerspectiveTransform(np.float32([[0, 0], [2334, 0], [2334, 1650], [0, 1650]]),
                  np.float32([[420, 260], [2900, 300], [2960, 1960], [360, 1900]]))
    cv2.warpPerspective(sheet, matrix, (3200, 2200), photo, borderMode=cv2.BORDER_TRANSPARENT)
    return photo


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

    def test_top_edge_follows_paper_down_to_the_fold(self):
        # the straight top edge would leave a wedge of background in the gutter-top corners
        # (unruled: the synthetic ruling is dark enough to count as pen strokes, which keep the edge)
        image, left, right = spread_photo(self.rng, dip=60, ruled=False)
        self.save('01-dip.jpg', image)
        pages = {p['id'][-1]: p for p in self.prepare()['pages']}
        matrix = cv2.getPerspectiveTransform(
            np.float32([[0, 0], [2 * PAGE_W, 0], [2 * PAGE_W, PAGE_H], [0, PAGE_H]]),
            np.float32([[420, 260], [2900, 300], [2960, 1960], [360, 1900]]))
        to_flat = np.linalg.inv(matrix)
        for side, corner, outer in (('L', 1, 0), ('R', 0, 1)):
            quad = np.float32(pages[side]['quad']) * [3200, 2200]
            flat = cv2.perspectiveTransform(quad.reshape(-1, 1, 2), to_flat)[:, 0]
            self.assertGreater(flat[corner][1], 8, f'{side}: gutter-top corner left on the background')
            self.assertLess(flat[corner][1], 75, f'{side}: gutter-top corner cut into the page')
            self.assertLess(abs(flat[outer][1]), 8, f'{side}: outer top corner moved')

    def test_landscape_printing_stays_single(self):
        for kind in ('text', 'table', 'grid'):
            self.save(kind + '.jpg', landscape_photo(kind))
        pages = self.prepare()['pages']
        self.assertEqual(len(pages), 3)
        self.assertTrue(all(p['id'].endswith('-P') for p in pages))

    def test_explicit_layout_overrides_fold_evidence(self):
        self.save('table.jpg', landscape_photo('table'))
        for layout, expected in (('pages', ['P']), ('spreads', ['L', 'R'])):
            work = self.tmp / layout
            run('prepare', self.photos, '--work', work, '--layout', layout)
            pages = json.loads((work / 'manifest.json').read_text())['pages']
            self.assertEqual([p['id'].split('-')[-1] for p in pages], expected)

    def test_prepare_jobs_identical(self):
        image, _, _ = spread_photo(self.rng, shadow=True)
        self.save('2.jpg', image)
        shutil.copyfile(self.photos / '2.jpg', self.photos / '10.jpg')
        manifests = []
        works = [self.tmp / 'serial', self.tmp / 'parallel']
        for jobs, work in zip((1, 2), works):
            run('prepare', self.photos, '--work', work, '--jobs', jobs)
            manifests.append(json.loads((work / 'manifest.json').read_text()))
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual([p['id'] for p in manifests[0]['pages']],
                         ['0001-L', '0001-R', '0002-L', '0002-R'])
        self.assertTrue(all('duplicate' in p['flags'] for p in manifests[0]['pages'][2:]))
        # Preview labels (including duplicate CHECK), outlines and contact sheets stay identical.
        for preview in works[0].glob('*.jpg'):
            self.assertEqual(preview.read_bytes(), (works[1] / preview.name).read_bytes())
        bad = run('prepare', self.photos, '--work', self.tmp / 'bad', '--jobs', 0, check=False)
        self.assertNotEqual(bad.returncode, 0)

    def test_cover_side_is_flagged(self):
        # unruled cardboard (inside of a cover) next to a page: both are paper-coloured
        image, _, _ = spread_photo(self.rng, cover_right=True)
        self.save('01-cover.jpg', image)
        manifest = self.prepare()
        flags = {f for p in manifest['pages'] for f in p['flags']}
        self.assertIn('one-side-unruled', flags)

    def test_margin_ink_widens_only_that_edge(self):
        # a curved outer edge stays tight, unless writing lies in the curved strip
        for name, date in (('01-plain.jpg', False), ('02-date.jpg', True)):
            image, left, _ = spread_photo(self.rng, bulge=70, date=date)
            self.save(name, image)
        pages = {(Path(p['source']).name, p['id'][-1]): p for p in self.prepare()['pages']}
        plain, dated = pages[('01-plain.jpg', 'L')], pages[('02-date.jpg', 'L')]
        self.assertNotIn('L-left', plain.get('margin_ink', []))
        self.assertIn('L-left', dated.get('margin_ink', []))
        def left_edge(page):          # x of the left edge at mid-height, photo px
            quad = np.float64(page['quad']) * [3200, 2200]
            return (quad[0][0] + quad[3][0]) / 2
        # the tight edge lies near the inner part of the 70 px arc (about 26 px out); the date sits
        # 2-50 px out and the widened edge keeps all of it
        straight = (left[0][0] + left[3][0]) / 2 * 3200
        self.assertGreater(left_edge(plain), straight - 35)
        self.assertLess(left_edge(dated), straight - 45)

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
