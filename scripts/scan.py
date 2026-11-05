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
    wide = 0
    for path in paths:
        with Image.open(path) as image:
            wide += image.width > image.height
    cols, rows, (cw, ch) = (4, 5, (480, 300)) if wide * 2 > len(paths) else (3, 2, (510, 630))
    per = cols * rows
    for start in range(0, len(paths), per):
        sheet = Image.new('RGB', (cols * (cw + 10), rows * (ch + 50)), '#ddd')
        draw = ImageDraw.Draw(sheet)
        for offset, path in enumerate(paths[start:start + per]):
            x, y = offset % cols * (cw + 10), offset // cols * (ch + 50)
            with Image.open(path) as image:
                thumb = ImageOps.contain(image, (cw, ch))
                sheet.paste(thumb, (x + (cw + 10 - thumb.width) // 2, y))
            draw.text((x + 8, y + ch + 12), str(start + offset + 1), fill='black')
        sheet.save(destination / f'{prefix}-{start // per + 1:03d}.jpg', quality=92)


def ordered(points):
    import numpy as np
    points = np.asarray(points, dtype='float32')
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    return np.roll(points, -np.argmin(points.sum(axis=1)), axis=0)


def paper_mask(small, saturation):
    """Largest bright low-saturation region with holes filled, or None."""
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
    mask = ((hsv[:, :, 1] < saturation) & (hsv[:, :, 2] > 110)).astype('uint8') * 255
    side = max(small.shape[:2])
    for operation, size in [(cv2.MORPH_CLOSE, side // 128 | 1), (cv2.MORPH_OPEN, side // 62 | 1)]:
        mask = cv2.morphologyEx(mask, operation, np.ones((size, size), 'uint8'))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if count < 2:
        return None
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[best, cv2.CC_STAT_AREA] < .08 * mask.size:
        return None
    contours, _ = cv2.findContours((labels == best).astype('uint8'), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_NONE)
    mask = np.zeros_like(mask)
    cv2.drawContours(mask, contours, -1, 255, -1)
    return mask


def outline(mask):
    import cv2
    import numpy as np
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    hull = cv2.convexHull(max(contours, key=cv2.contourArea))
    perimeter = cv2.arcLength(hull, True)
    for epsilon in np.linspace(.005, .1, 80):
        quad = cv2.approxPolyDP(hull, epsilon * perimeter, True)
        if len(quad) == 4:
            return ordered(quad[:, 0])
    return ordered(cv2.boxPoints(cv2.minAreaRect(hull)))


def fit(t, v, inward, percentile=90):
    """Robust line v = a*t + b, moved inward until `percentile`% of edge samples lie outside it."""
    import numpy as np
    t, v = np.asarray(t, float), np.asarray(v, float)
    keep = np.ones(len(t), bool)
    for _ in range(5):
        if keep.sum() < 20:
            return None
        a, b = np.polyfit(t[keep], v[keep], 1)
        residual = v - (a * t + b)
        spread = 1.4826 * np.median(np.abs(residual[keep] - np.median(residual[keep]))) + .5
        keep = np.abs(residual) < 3 * spread
    if keep.sum() < 20:
        return None
    return a, b + inward * np.percentile(inward * residual[keep], percentile)


def meet(horizontal, vertical):
    """Intersection of y = a*x + b with x = c*y + d."""
    (a, b), (c, d) = horizontal, vertical
    y = (a * d + b) / (1 - a * c)
    return [c * y + d, y]


def refine(small, full, mask, quad, spread):
    """Per-page quads from edge lines in the rectified draft frame.

    Top and outer edges: tight (90% of the paper edge lies outside the line); bottom: median line,
    because writing often reaches the lower edge. Gutter: the shadow valley is usually on one page,
    so the fold is the steepest brightness step next to it, measured on the full-resolution photo.
    """
    import cv2
    import numpy as np
    width = (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2
    height = (np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2
    W = 2400 if spread else 1600
    H = max(200, round(W * height / max(width, 1)))
    mx, my = round(.06 * W), round(.06 * H)
    frame = np.float32([[mx, my], [mx + W, my], [mx + W, my + H], [mx, my + H]])
    matrix = cv2.getPerspectiveTransform(np.float32(quad), frame)
    size = (W + 2 * mx, H + 2 * my)
    sheet = cv2.warpPerspective(mask, matrix, size, flags=cv2.INTER_NEAREST) > 127
    def edge(name, start, stop):
        steps = np.arange(round(start), round(stop), 4)
        if name == 'top':
            band = sheet[:my + round(.12 * H), steps]
            found = band.argmax(0)
        elif name == 'bottom':
            low = my + H - round(.12 * H)
            band = sheet[low:, steps]
            found = low + band.shape[0] - 1 - band[::-1].argmax(0)
        elif name == 'left':
            band = sheet[steps, :mx + round(.1 * W)].T
            found = band.argmax(0)
        else:
            low = mx + W - round(.1 * W)
            band = sheet[steps, low:].T
            found = low + band.shape[0] - 1 - band[::-1].argmax(0)
        hit = band.any(0)
        return steps[hit], found[hit]
    left = fit(*edge('left', my + .08 * H, my + .92 * H), 1)
    right = fit(*edge('right', my + .08 * H, my + .92 * H), -1)
    info = {}
    if spread:
        scale = small.shape[1] / full.shape[1]
        gray = cv2.cvtColor(cv2.warpPerspective(full, matrix @ np.diag([scale, scale, 1]), size),
                            cv2.COLOR_RGB2GRAY).astype('float32')
        def column(a, b, sigma):
            row = np.median(gray[my + round(a * H):my + round(b * H)], axis=0)
            return cv2.GaussianBlur(row.reshape(1, -1), (0, 0), sigma).ravel()
        def valley(row, low, high, reach=30):
            depth = row[low:high] - (row[low - reach:high - reach] + row[low + reach:high + reach]) / 2
            return low + int(np.argmin(depth)), float(depth.min())
        centre, depth = valley(column(.1, .9, 3), mx + round(.4 * W), mx + round(.6 * W))
        info['gutter_contrast'] = round(-depth, 1)
        reach, ys, xs = round(.025 * W), [], []
        for a, b in [(.03, .25), (.4, .6), (.75, .97)]:
            shadow = valley(column(a, b, 3), centre - reach, centre + reach)[0]
            step = np.abs(np.gradient(column(a, b, 2))[shadow - 45:shadow + 45])
            xs.append(shadow - 45 + int(np.argmax(step)))
            ys.append(my + (a + b) / 2 * H)
        ys, xs = np.float64(ys), np.float64(xs)
        slope, offset = np.polyfit(ys, xs, 1)
        error = np.abs(xs - (slope * ys + offset))
        if error.max() > 12:
            best = np.argsort(error)[:2]
            slope, offset = np.polyfit(ys[best], xs[best], 1)
        overlap = .0015 * W
        spans = [('L', mx + .04 * W, min(xs) - .07 * W, left, (slope, offset + overlap)),
                 ('R', max(xs) + .07 * W, mx + .96 * W, (slope, offset - overlap), right)]
    else:
        spans = [('P', mx + .06 * W, mx + .94 * W, left, right)]
    inverse = np.linalg.inv(matrix)
    pages = []
    for side, start, stop, outer_left, outer_right in spans:
        top = fit(*edge('top', start, stop), 1)
        bottom = fit(*edge('bottom', start, stop), -1, 50)
        if None in (top, bottom, outer_left, outer_right):
            return None, info
        corners = [meet(top, outer_left), meet(top, outer_right),
                   meet(bottom, outer_right), meet(bottom, outer_left)]
        points = cv2.perspectiveTransform(np.float32(corners).reshape(-1, 1, 2), inverse)[:, 0]
        if not cv2.isContourConvex(points) or cv2.contourArea(points) < .25 * cv2.contourArea(np.float32(quad)):
            return None, info
        pages.append((side, points))
    return pages, info


def guess(image, layout):
    """Draft per-page quads (normalized) for one photo: [(side, quad)], method, extra fields."""
    import cv2
    import numpy as np
    w, h = image.size
    full = np.array(image)
    scale = min(1.0, 1600 / max(w, h))
    small = cv2.resize(full, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    norm = np.float32([small.shape[1], small.shape[0]])
    for saturation in [60, 145]:
        mask = paper_mask(small, saturation)
        if mask is not None:
            break
    if mask is None:
        quad = np.float32([[0, 0], [1, 0], [1, 1], [0, 1]]) * norm
        method = 'full-frame-fallback'
    else:
        quad, method = outline(mask), 'contour'
    ratio = np.linalg.norm(quad[1] - quad[0]) / max(np.linalg.norm(quad[3] - quad[0]), 1)
    spread = layout == 'spreads' or (layout == 'auto' and ratio > 1.25)
    extra = {}
    if mask is not None:
        try:
            pages, extra = refine(small, full, mask, quad, spread)
        except (ValueError, IndexError, ZeroDivisionError, np.linalg.LinAlgError, cv2.error):
            pages = None
        if pages:
            method = 'edges+gutter' if spread else 'edges'
            if spread and extra.get('gutter_contrast', 0) < 10:
                method += '-uncertain'
            return [(s, q / norm) for s, q in pages], method, extra
    if spread:
        tl, tr, br, bl = quad
        top, bottom = (tl + tr) / 2, (bl + br) / 2
        pages = [('L', np.array([tl, top, bottom, bl])), ('R', np.array([top, tr, br, bottom]))]
    else:
        pages = [('P', quad)]
    return [(s, q / norm) for s, q in pages], method, extra


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
        choices, method, extra = guess(image, args.layout)
        checksum = digest(path)
        image.thumbnail((1100, 1100))
        draw = ImageDraw.Draw(image)
        for side, points in choices:
            item = dict(id=f'{number:04d}-{side}', source=str(path.resolve()), sha256=checksum,
                        quad=np.round(points, 5).tolist(), aspect=None, bleed_px=0, enhance=True,
                        include=True, reviewed=False, detection=method, **extra)
            if checksum in seen:
                item['duplicate_of'] = seen[checksum]
            pages.append(item)
            outline = [tuple(p * image.size) for p in points]
            colour = {'L': (255, 40, 20), 'R': (0, 170, 0)}.get(side, (230, 0, 230))
            draw.line(outline + outline[:1], fill=colour, width=3)
            draw.text(outline[0], item['id'], fill=colour)
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
    if args.width_mm is not None and not 10 <= args.width_mm <= 2000:
        fail('--width-mm must be 10..2000.')
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
        width_mm = page.get('width_mm')
        if width_mm is not None and (isinstance(width_mm, bool) or not isinstance(width_mm, (int, float))
                                     or not 10 <= width_mm <= 2000):
            fail(f'width_mm must be a number 10..2000: {page["id"]}')
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
        Image.fromarray(result).save(target, quality=args.quality, subsampling=0)
        width_mm = args.width_mm if page.get('width_mm') is None else page['width_mm']
        if width_mm:
            size = (width_mm * 72 / 25.4, width_mm * 72 / 25.4 * result.shape[0] / result.shape[1])
        else:
            size = (result.shape[1] * 72 / args.dpi, result.shape[0] * 72 / args.dpi)
        pdf.setPageSize(size)
        pdf.drawImage(str(target), 0, 0, *size)
        pdf.showPage()
        record.append(dict(page=number, id=page['id'], source=page['source'],
                           sha256=checksums[str(Path(page['source']).resolve())],
                           matrix=matrix.tolist(), pixels=[result.shape[1], result.shape[0]],
                           points=[round(size[0], 2), round(size[1], 2)]))
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


def inspect(args):
    """Per photo: overview with page outlines + 1:1 crops around every corner, ticks in normalized units."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    data = load(args.manifest)
    wanted = set(args.ids.split(',')) if args.ids else None
    groups = {}
    for page in data['pages']:
        if (wanted is None and page.get('include', True)) or (wanted and page['id'] in wanted):
            groups.setdefault(page['source'], []).append(page)
    if not groups:
        fail('No matching pages.')
    work = work_dir(args.work, args.replace)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()
    colours = [(255, 0, 0), (0, 190, 0), (230, 0, 230), (0, 120, 255)]
    half, side = args.half, 2 * args.half
    for source, pages in groups.items():
        image = photo(source)
        w, h = image.size
        draw = ImageDraw.Draw(image)
        corners = []
        for k, page in enumerate(pages):
            quad = np.float64(page['quad']) * [w, h]
            draw.line([tuple(p) for p in quad] + [tuple(quad[0])], fill=colours[k % 4], width=2)
            for point in quad:
                if all(np.linalg.norm(point - c) > half / 2 for c in corners):
                    corners.append(point)
        tiles = []
        for c in corners:
            x0, y0 = round(c[0]) - half, round(c[1]) - half
            tile = Image.new('RGB', (side, side), (60, 60, 60))
            tile.paste(image.crop((x0, y0, x0 + side, y0 + side)))
            mark = ImageDraw.Draw(tile)
            for g in range((x0 // 50 + 1) * 50, x0 + side, 50):
                mark.line([(g - x0, 0), (g - x0, 16 if g % 100 == 0 else 8)], fill=(255, 255, 0))
                if g % 100 == 0:
                    mark.text((g - x0 + 2, 16), f'{g / w:.3f}', fill=(255, 255, 0), font=font)
            for g in range((y0 // 50 + 1) * 50, y0 + side, 50):
                mark.line([(0, g - y0), (16 if g % 100 == 0 else 8, g - y0)], fill=(255, 255, 0))
                if g % 100 == 0:
                    mark.text((18, g - y0 - 9), f'{g / h:.3f}', fill=(255, 255, 0), font=font)
            mark.text((4, side - 22), f'({c[0] / w:.4f}, {c[1] / h:.4f})', fill=(0, 255, 255), font=font)
            tiles.append(tile)
        columns = 3
        overview = image.copy()
        overview.thumbnail((side * columns, side * columns))
        sheet = Image.new('RGB', (side * columns, overview.height + -(-len(tiles) // columns) * side), (30, 30, 30))
        sheet.paste(overview, ((sheet.width - overview.width) // 2, 0))
        legend = ImageDraw.Draw(sheet)
        for k, page in enumerate(pages):
            legend.text((8, 8 + 22 * k), page['id'], fill=colours[k % 4], font=font)
        for i, tile in enumerate(tiles):
            sheet.paste(tile, (i % columns * side, overview.height + i // columns * side))
        name = '_'.join(re.sub(r'[^\w.-]', '-', page['id']) for page in pages)
        sheet.save(work / f'inspect-{name}.jpg', quality=90)
    print(f'{len(groups)} photographs -> {work}; ticks are normalized quad units (x/width, y/height).')


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError('Must be positive.')
    return value


def quality(value):
    value = int(value)
    if not 60 <= value <= 100:
        raise argparse.ArgumentTypeError('JPEG quality must be 60..100.')
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
    p.add_argument('--quality', type=quality, default=95, help='JPEG quality 60..100, 4:4:4')
    p.add_argument('--width-mm', type=float, help='Physical page width for pages without width_mm')
    p.set_defaults(run=build)
    p = subs.add_parser('crop', help='Crop PDF boxes without recompressing embedded images')
    p.add_argument('input', type=Path)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.set_defaults(run=crop)
    p = subs.add_parser('inspect', help='Corner close-ups of manifest quads for manual correction')
    p.add_argument('manifest', type=Path)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--ids', help='Comma-separated page ids; default: all included pages')
    p.add_argument('--half', type=positive, default=200, help='Half crop size in source pixels')
    p.set_defaults(run=inspect)
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
