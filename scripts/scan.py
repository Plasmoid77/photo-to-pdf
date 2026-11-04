#!/usr/bin/env python3
"""Reviewed photo quads to PDF; PDF box cropping without image recompression."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess


def fail(message):
    raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def target_file(path, replace):
    path = Path(path).resolve()
    if path.suffix.lower() != '.pdf':
        fail('PDF output must have a .pdf extension.')
    if path.exists() and not replace:
        fail(f'Output exists: {path}; use a fresh destination or --replace.')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def work_dir(path, replace):
    path = Path(path).resolve()
    marker = path / '.photo-to-pdf-work'
    if path.exists() and any(path.iterdir()):
        if not replace or not marker.is_file():
            fail(f'Use an empty working directory, or --replace for a directory created by this script: {path}')
    path.mkdir(parents=True, exist_ok=True)
    marker.write_text('photo-to-pdf\n', encoding='utf-8')
    return path


def photo(path):
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        if getattr(image, 'n_frames', 1) > 1:
            fail(f'Split multi-frame image before processing: {path}')
        return ImageOps.exif_transpose(image).convert('RGB')


def contact(paths, destination, prefix):
    from PIL import Image, ImageDraw, ImageOps
    for path in destination.glob(f'{prefix}-[0-9][0-9][0-9].jpg'):
        path.unlink()
    for start in range(0, len(paths), 6):
        sheet = Image.new('RGB', (1560, 1360), '#ddd')
        draw = ImageDraw.Draw(sheet)
        for offset, path in enumerate(paths[start:start + 6]):
            with Image.open(path) as image:
                thumb = ImageOps.contain(image, (510, 630))
                x, y = offset % 3 * 520, offset // 3 * 680
                sheet.paste(thumb, (x + (520 - thumb.width) // 2, y))
            draw.text((x + 8, y + 642), str(start + offset + 1), fill='black')
        sheet.save(destination / f'{prefix}-{start // 6 + 1:03d}.jpg', quality=92)


def ordered(points):
    import numpy as np
    points = np.asarray(points, dtype='float32')
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    return np.roll(points, -np.argmin(points.sum(axis=1)), axis=0)


def guess(image):
    import cv2
    import numpy as np
    w, h = image.size
    scale = min(1.0, 1200 / max(w, h))
    small = cv2.resize(np.array(image), None, fx=scale, fy=scale)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    mask = ((hsv[:, :, 2] > 110) & (hsv[:, :, 1] < 145)).astype('uint8') * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), 'uint8'))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        contour = max(contours, key=cv2.contourArea)
        if cv2.contourArea(contour) > small.shape[0] * small.shape[1] * .08:
            for epsilon in [.01, .02, .04]:
                quad = cv2.approxPolyDP(contour, epsilon * cv2.arcLength(contour, True), True)
                if len(quad) == 4 and cv2.isContourConvex(quad):
                    return ordered(quad[:, 0] / scale) / [w, h], 'contour'
    return np.float32([[0, 0], [1, 0], [1, 1], [0, 1]]), 'full-frame-fallback'


def prepare(args):
    import numpy as np
    from PIL import ImageDraw
    work = work_dir(args.work, args.replace)
    root = args.input.resolve()
    if not root.is_dir():
        fail('prepare expects a directory of photographs.')
    if root == work:
        fail('Keep the working directory separate from source photographs.')
    extensions = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp'}
    def key(path):
        return [int(s) if s.isdigit() else s.lower() for s in re.split(r'(\d+)', path.name)]
    sources = sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in extensions), key=key)
    if not sources:
        fail(f'No supported photographs: {root}')
    pages, previews, seen = [], [], {}
    for number, path in enumerate(sources, 1):
        image = photo(path)
        quad, method = guess(image)
        pix = quad * image.size
        ratio = np.linalg.norm(pix[1] - pix[0]) / max(np.linalg.norm(pix[3] - pix[0]), 1)
        layout = args.layout if args.layout != 'auto' else ('spreads' if ratio > 1.25 else 'pages')
        choices = [('P', quad)]
        if layout == 'spreads':
            tl, tr, br, bl = quad
            top, bottom = (tl + tr) / 2, (bl + br) / 2
            choices = [('L', np.array([tl, top, bottom, bl])),
                       ('R', np.array([top, tr, br, bottom]))]
        checksum = digest(path)
        image.thumbnail((1100, 1100))
        draw = ImageDraw.Draw(image)
        for side, points in choices:
            item = dict(id=f'{number:04d}-{side}', source=str(path.resolve()), sha256=checksum,
                        quad=points.tolist(), aspect=None, bleed_px=0, enhance=True,
                        include=True, reviewed=False, detection=method)
            if checksum in seen:
                item['duplicate_of'] = seen[checksum]
            pages.append(item)
            outline = [tuple(p * image.size) for p in points]
            draw.line(outline + outline[:1], fill=(255, 40, 20), width=3)
            draw.text(outline[0], item['id'], fill=(255, 30, 20))
        seen[checksum] = str(path.resolve())
        preview = work / f'source-{number:03d}.jpg'
        image.save(preview, quality=92)
        previews.append(preview)
    dump(work / 'manifest.json', dict(version=1, pages=pages))
    contact(previews, work, 'sources')
    print(f'{len(sources)} photographs; {len(pages)} draft pages. Review manifest and outlines.')


def light(array):
    import cv2
    import numpy as np
    height, width = array.shape[:2]
    small = cv2.resize(array, (320, max(32, round(320 * height / width))))
    background = cv2.morphologyEx(small, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19, 19)))
    background = cv2.GaussianBlur(background, (0, 0), 15)
    background = cv2.resize(background, (width, height))
    result = array.astype('float32') / np.maximum(background.astype('float32'), 100) * 247
    return np.clip(255 - (255 - result) * 1.12, 0, 255).astype('uint8')


def build(args):
    import cv2
    import numpy as np
    from PIL import Image
    from reportlab import rl_config
    from reportlab.pdfgen import canvas
    rl_config.useA85 = False
    data = load(args.manifest)
    if data.get('version') != 1:
        fail('Unsupported manifest version.')
    pages = [p for p in data['pages'] if p.get('include', True)]
    if not pages or any(p.get('reviewed') is not True for p in pages):
        fail('Visually review every included page, then set reviewed=true.')
    checksums = {}
    for page in pages:
        source = str(Path(page['source']).resolve())
        if source not in checksums:
            checksums[source] = digest(source)
        if page.get('sha256') != checksums[source]:
            fail(f'Original changed since prepare; recheck its quad: {source}')
        quad = np.asarray(page['quad'], dtype='float32')
        if quad.shape != (4, 2) or not np.isfinite(quad).all() or (quad < -.1).any() or (quad > 1.1).any():
            fail(f'Invalid normalized quad: {page["id"]}')
        if not cv2.isContourConvex(quad) or abs(cv2.contourArea(quad)) < .001:
            fail(f'Degenerate or crossing quad: {page["id"]}')
        aspect, bleed = page.get('aspect'), page.get('bleed_px', 0)
        if aspect is not None and (not math.isfinite(float(aspect)) or not .1 < float(aspect) < 10):
            fail(f'Invalid aspect: {page["id"]}')
        if isinstance(bleed, bool) or not isinstance(bleed, int) or not 0 <= bleed <= 100:
            fail(f'bleed_px must be an integer 0..100: {page["id"]}')
    output = target_file(args.output, args.replace)
    images = work_dir(output.parent / f'{output.stem}-pages', args.replace)
    temporary = output.with_name(output.name + '.partial')
    pdf = canvas.Canvas(str(temporary), pageCompression=1)
    pdf.setTitle(output.stem)
    record, last_source, source_array = [], None, None
    for number, page in enumerate(pages, 1):
        if page['source'] != last_source:
            source_array = np.array(photo(page['source']))
            last_source = page['source']
        h, w = source_array.shape[:2]
        quad = np.float32(page['quad']) * [w, h]
        width = (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2
        height = (np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2
        if page.get('aspect') is not None:
            width = math.sqrt(width * height * float(page['aspect']))
            height = width / float(page['aspect'])
        scale = min(1, args.max_edge / max(width, height))
        width, height = max(16, round(width * scale)), max(16, round(height * scale))
        bleed = page.get('bleed_px', 0)
        dest = np.float32([[bleed, bleed], [bleed + width - 1, bleed],
                          [bleed + width - 1, bleed + height - 1], [bleed, bleed + height - 1]])
        matrix = cv2.getPerspectiveTransform(quad.astype('float32'), dest)
        result = cv2.warpPerspective(source_array, matrix, (width + 2 * bleed, height + 2 * bleed),
                                     flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
        if page.get('enhance', True):
            result = light(result)
        target = images / f'{number:04d}.jpg'
        Image.fromarray(result).save(target, quality=95, subsampling=0)
        size = (result.shape[1] * 72 / args.dpi, result.shape[0] * 72 / args.dpi)
        pdf.setPageSize(size)
        pdf.drawImage(str(target), 0, 0, *size)
        pdf.showPage()
        record.append(dict(page=number, id=page['id'], source=page['source'],
                           sha256=checksums[str(Path(page['source']).resolve())],
                           matrix=matrix.tolist(), pixels=[result.shape[1], result.shape[0]]))
    pdf.save()
    temporary.replace(output)
    dump(output.with_suffix('.scan.json'), dict(version=1, pages=record))
    print(f'{len(pages)} pages -> {output}; render and inspect before delivery.')


def crop(args):
    import pypdfium2 as pdfium
    plan = load(args.plan)
    source = args.input.resolve()
    output = target_file(args.output, args.replace)
    if source == output:
        fail('Write cropped PDF separately; replace original after verification.')
    document = pdfium.PdfDocument(source)
    for key, rect in plan.items():
        if not key.isdecimal() or str(int(key)) != key or not 1 <= int(key) <= len(document):
            fail(f'Invalid page number: {key}')
        if len(rect) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in rect):
            fail(f'Invalid crop rectangle: {key}')
        l, t, r, b = rect
        if not 0 <= l < r <= 1 or not 0 <= t < b <= 1:
            fail(f'Invalid crop bounds: {key}; require 0 <= left < right <= 1, 0 <= top < bottom <= 1.')
    hashes = []
    def image_hashes(page):
        return [hashlib.sha256(bytes(o.get_data())).hexdigest()
                for o in page.get_objects() if isinstance(o, pdfium.PdfImage)]
    for number in range(len(document)):
        page = document[number]
        hashes.append(image_hashes(page))
        if str(number + 1) in plan:
            if page.get_rotation() != 0:
                fail(f'Normalize PDF rotation before cropping page {number + 1}.')
            x0, y0, x1, y1 = page.get_bbox()
            l, t, r, b = plan[str(number + 1)]
            box = (x0 + l * (x1 - x0), y1 - b * (y1 - y0),
                   x0 + r * (x1 - x0), y1 - t * (y1 - y0))
            for method in ['set_mediabox', 'set_cropbox', 'set_trimbox', 'set_bleedbox', 'set_artbox']:
                getattr(page, method)(*box)
        page.close()
    temporary = output.with_name(output.name + '.partial')
    document.save(temporary)
    document.close()
    check = pdfium.PdfDocument(temporary)
    assert len(check) == len(hashes), 'Page count changed during crop.'
    for number in range(len(check)):
        page = check[number]
        assert image_hashes(page) == hashes[number], f'Image stream changed: page {number + 1}'
        page.close()
    check.close()
    temporary.replace(output)
    print(f'{len(hashes)} pages; image streams unchanged -> {output}')


def review(args):
    executable = shutil.which('pdftoppm')
    if not executable:
        fail('Install/use Poppler pdftoppm for final PDF visual verification.')
    work = work_dir(args.work, args.replace)
    render = work / 'render'
    render.mkdir(exist_ok=True)
    for path in render.glob('page-*.jpg'):
        path.unlink()
    subprocess.run([executable, '-scale-to', str(args.max_edge), '-jpeg', '-jpegopt',
                    'quality=92', str(args.input.resolve()), str(render / 'page')], check=True)
    paths = sorted(render.glob('page-*.jpg'))
    if not paths:
        fail('No pages rendered.')
    contact(paths, work, 'pages')
    print(f'{len(paths)} pages rendered; inspect ALL sheets and individual edge exceptions: {work}')


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError('Must be positive.')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('prepare', help='Draft photo-quads manifest and source contact sheets')
    p.add_argument('input', type=Path)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--layout', choices=['auto', 'pages', 'spreads'], default='auto')
    p.set_defaults(run=prepare)
    p = subs.add_parser('build', help='Create PDF from visually reviewed quads')
    p.add_argument('manifest', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--max-edge', type=positive, default=2800)
    p.add_argument('--dpi', type=positive, default=240)
    p.set_defaults(run=build)
    p = subs.add_parser('crop', help='Crop PDF boxes without recompressing embedded images')
    p.add_argument('input', type=Path)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.set_defaults(run=crop)
    p = subs.add_parser('review', help='Render every PDF page for visual verification')
    p.add_argument('input', type=Path)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--max-edge', type=positive, default=1400)
    p.set_defaults(run=review)
    for p in subs.choices.values():
        p.add_argument('--replace', action='store_true', help='Allow replacing task outputs, never source photos')
    args = parser.parse_args()
    try:
        args.run(args)
    except (ValueError, OSError, ImportError) as error:
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    main()
