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
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            value.update(block)
    return value.hexdigest()


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
    """Robust line v = a*t + b, moved inward until `percentile`% of edge samples lie outside it.

    Outliers (fingers, shadows, notches) are rejected iteratively, but at least 30 % of the samples
    stay in the fit, so a gently curved edge is approximated instead of rejected.
    """
    import numpy as np
    t, v = np.asarray(t, float), np.asarray(v, float)
    if len(t) < 20:
        return None
    keep = np.ones(len(t), bool)
    for _ in range(5):
        a, b = np.polyfit(t[keep], v[keep], 1)
        residual = v - (a * t + b)
        spread = max(1.4826 * np.median(np.abs(residual[keep] - np.median(residual[keep]))), 2.)
        narrower = np.abs(residual) < 3 * spread
        if narrower.sum() < max(20, .3 * len(t)):
            break
        keep = narrower
    a, b = np.polyfit(t[keep], v[keep], 1)
    residual = v - (a * t + b)
    return a, b + inward * np.percentile(inward * residual[keep], percentile)


def meet(horizontal, vertical):
    """Intersection of y = a*x + b with x = c*y + d."""
    (a, b), (c, d) = horizontal, vertical
    y = (a * d + b) / (1 - a * c)
    return [c * y + d, y]


CREASE_BANDS = ((.04, .24), (.40, .60), (.76, .96))


def crease(full, quad):
    """Gutter of a two-page spread.

    `quad` is the spread outline (TL, TR, BR, BL) in full-resolution pixels. Within the central
    band of the rectified spread the darkest straight (possibly tilted) line is found on the blue
    channel with ink and bluish ruling suppressed. On a thin notebook that line is the crease
    itself. When it lies in a deep shadow, the crease is the foot of the steepest brightness step
    next to it, on its dark side: the shadow belongs to one page and the step is where the other
    page begins (30 % of the peak slope). Returns (top, bottom) points in full-resolution pixels, info and flags.
    """
    import cv2
    import numpy as np
    quad = np.float32(quad)
    W = int(round((np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2))
    H = int(round((np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2))
    matrix = cv2.getPerspectiveTransform(quad, np.float32([[0, 0], [W, 0], [W, H], [0, H]]))
    low, high, pad = int(.36 * W), int(.64 * W), 60
    shift = np.array([[1, 0, pad - low], [0, 1, 0], [0, 0, 1]], float) @ matrix
    band = cv2.warpPerspective(full, shift, (high - low + 2 * pad, H), flags=cv2.INTER_AREA)
    n = band.shape[1]
    X = np.arange(n) + low - pad
    hsv = cv2.cvtColor(band, cv2.COLOR_RGB2HSV)
    blue = band[..., 2].astype('float32')
    ratio = blue / np.maximum(band[..., 0].astype('float32'), 1)
    neutral = cv2.medianBlur(np.clip(ratio * 200, 0, 255).astype('uint8'), 31).astype('float32') / 200
    ink = (hsv[..., 1] > 70) | (ratio > neutral + .012)
    plain = blue.copy()
    plain[ink] = cv2.medianBlur(band[..., 2], 21).astype('float32')[ink]
    plain = cv2.GaussianBlur(plain, (0, 0), 1.2)
    background = np.zeros_like(plain)
    for radius in (6, 12, 30):
        side = np.zeros_like(plain)
        side[:, radius:-radius] = (plain[:, :-2 * radius] + plain[:, 2 * radius:]) / 2
        background = np.maximum(background, side)
    response = cv2.blur(np.clip(background - plain, 0, 60), (5, 1))
    response[:, :pad] = 0
    response[:, -pad:] = 0
    rows = np.arange(int(.04 * H), int(.96 * H), 4)
    sampled = response[rows]
    tilts = np.arange(-int(.05 * W), int(.05 * W) + 1, 2)
    table = np.zeros((len(tilts), n))
    for i, tilt in enumerate(tilts):
        # mean along the tilted line: rows with the same rounded shift move as one block
        shift = np.round(tilt * rows / H).astype(int)
        moved = np.zeros_like(sampled)
        for step in np.unique(shift):
            group = shift == step
            if step >= 0:
                moved[group, :n - step] = sampled[group, step:]
            else:
                moved[group, -step:] = sampled[group, :n + step]
        table[i] = moved.mean(0)
    best = table.argmax(0)
    centre = X + tilts[best] / 2
    score = table.max(0) * np.exp(-.5 * ((centre - .5 * W) / (.03 * W)) ** 2)
    k = int(np.argmax(score))
    tilt = int(tilts[best[k]])
    left, right = k, k
    while left > 0 and score[left - 1] > .7 * score[k]:
        left -= 1
    while right < n - 1 and score[right + 1] > .7 * score[k]:
        right += 1
    rest = np.concatenate([score[:max(0, left - 8)], score[right + 9:]])
    ambiguity = float(rest.max() / max(score[k], 1e-9)) if rest.size else 0.
    line = np.round(k + tilt * rows / H).astype(int)
    valid = (line > 32) & (line < n - 32)
    depth = float(np.median(((plain[rows, line - 30] + plain[rows, line + 30]) / 2
                             - plain[rows, line])[valid])) if valid.any() else 0.
    xs, ys = [], []
    for top, bottom in CREASE_BANDS:
        y = (top + bottom) / 2 * H
        x = int(round(k + tilt * y / H))
        if depth >= 15:
            profile = np.median(plain[int(top * H):int(bottom * H)], axis=0)
            gradient = np.gradient(cv2.GaussianBlur(profile.reshape(1, -1), (0, 0), 2).ravel())
            a, b = max(2, x - 45), min(n - 2, x + 46)
            q = a + int(np.argmax(np.abs(gradient[a:b])))
            sign, peak = np.sign(gradient[q]), abs(gradient[q])
            step = -1 if sign > 0 else 1
            while a < q < b and sign * gradient[q] > .3 * peak:
                q += step
            x = q
        xs.append(X[min(max(x, 0), n - 1)])
        ys.append(y)
    xs, ys = np.float64(xs), np.float64(ys)
    slope, offset = np.polyfit(ys, xs, 1)
    error = np.abs(xs - (slope * ys + offset))
    disagree = error.max() > .008 * W
    if error.max() > .004 * W:
        keep = np.argsort(error)[:2]
        slope, offset = np.polyfit(ys[keep], xs[keep], 1)
    flags = []
    if abs(slope * H / 2 + offset - .5 * W) > .07 * W:
        flags.append('gutter-off-centre')
    if ambiguity > .85:
        flags.append('gutter-ambiguous')
    if disagree:
        flags.append('gutter-bands-disagree')
    gray = cv2.warpPerspective(cv2.cvtColor(full, cv2.COLOR_RGB2GRAY), matrix, (W, H),
                               flags=cv2.INTER_AREA).astype('float32')
    detail = gray - cv2.GaussianBlur(gray, (0, 0), 8)
    def ruling(a, b):
        return float(detail[int(.1 * H):int(.9 * H), int(a * W):int(b * W)].mean(1).std())
    texture = sorted([ruling(.12, .4), ruling(.6, .88)])
    if texture[0] < .35 * texture[1]:
        flags.append('one-side-unruled')
    ends = np.float32([[offset, 0], [slope * H + offset, H]]).reshape(-1, 1, 2)
    top, bottom = cv2.perspectiveTransform(ends, np.linalg.inv(matrix))[:, 0]
    info = dict(gutter_method='shadow' if depth >= 15 else 'line', gutter_depth=round(depth, 1),
                gutter_ambiguity=round(ambiguity, 2))
    return top, bottom, info, flags


def refine(small, full, mask, quad, spread, margin=.06):
    """Per-page quads from paper edges in the rectified draft frame.

    Paper-mask edge samples are snapped to the strongest step from paper to a clearly darker
    background within 2 % of the frame width (pale stacked sheets or skin keep the mask edge), then fitted with robust straight lines: top and outer sides
    tight (90 % of the edge outside the line), bottom by the median, because writing often reaches
    the lower edge. A fitted edge then moves outward over pen strokes lying between it and the
    outermost paper line (by at most 4 % of the page width; beyond that the page is flagged
    `margin-ink`). For a spread, `crease` finds the gutter inside the fitted outline; both pages
    overlap it slightly. When the paper of a curved page runs past the frame around the draft
    outline, the search is repeated with a wider frame.
    """
    import cv2
    import numpy as np
    width = (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2
    height = (np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2
    W = 2400 if spread else 1600
    H = max(200, round(W * height / max(width, 1)))
    mx, my = round(margin * W), round(margin * H)
    frame = np.float32([[mx, my], [mx + W, my], [mx + W, my + H], [mx, my + H]])
    matrix = cv2.getPerspectiveTransform(np.float32(quad), frame)
    size = (W + 2 * mx, H + 2 * my)
    scale = small.shape[1] / full.shape[1]
    to_frame = matrix @ np.diag([scale, scale, 1])
    sheet = cv2.warpPerspective(mask, matrix, size, flags=cv2.INTER_NEAREST) > 127
    gray = cv2.GaussianBlur(cv2.warpPerspective(cv2.cvtColor(full, cv2.COLOR_RGB2GRAY), to_frame, size,
                                                flags=cv2.INTER_AREA).astype('float32'), (0, 0), 1.5)
    # pen strokes: thin, darker than the paper around them and bluer than its local tint (ruling is
    # pale, the red margin line is not bluer, the paper tint follows the lighting, a cover edge is wide)
    colour = cv2.warpPerspective(full, to_frame, size, flags=cv2.INTER_AREA)
    tint = colour[..., 2].astype('float32') - colour[..., 1]
    ink = ((cv2.morphologyEx(cv2.cvtColor(colour, cv2.COLOR_RGB2GRAY), cv2.MORPH_BLACKHAT,
                             np.ones((11, 11), 'uint8')) > 40)
           & (tint - cv2.blur(tint, (41, 41)) > 15)
           & (cv2.erode(sheet.astype('uint8'), np.ones((9, 9), 'uint8')) > 0))
    ink_y, ink_x = np.nonzero(ink)
    reach = round(.02 * W)
    widened, clipped, capped = [], [], []
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
        steps, found = steps[hit], found[hit]
        if hit.any():
            border = {'top': 0, 'left': 0, 'bottom': sheet.shape[0] - 1, 'right': sheet.shape[1] - 1}[name]
            clipped.append(float(np.mean(found == border)))
        outward = -1 if name in ('top', 'left') else 1
        along = gray if name in ('left', 'right') else gray.T
        limit = along.shape[1] - 1
        offsets = np.arange(-reach, reach + 1)
        position = np.clip(found[:, None] + offsets[None, :], 4, limit - 4)
        rows = steps[:, None]
        inner = along[rows, position - 3 * outward]
        outer = along[rows, position + 3 * outward]
        contrast = np.where(outer < .75 * inner, inner - outer, 0)
        best = contrast.argmax(1)
        strong = contrast[np.arange(len(steps)), best] >= 25
        found = np.where(strong, position[np.arange(len(steps)), best], found)
        return steps, found
    def guard(line, samples, inward, name, start, stop, label):
        """Move a fitted edge outward over blue ink lying between it and the outermost paper line."""
        if line is None:
            return None
        a, b = line
        outward = -inward
        room = outward * (fit(*samples, inward, 5)[1] - b)
        t, v = (ink_x, ink_y) if name in ('top', 'bottom') else (ink_y, ink_x)
        span = (t >= start) & (t < stop)
        d = outward * (v[span] - (a * t[span] + b))
        d = d[(d > 0) & (d <= room)]
        if room < 2 or d.size < 15:
            return line
        widened.append(label)
        shift = np.percentile(d, 98) + 4
        cap = .04 * (W / 2 if spread else W)
        if min(shift, room) > cap:
            capped.append(label)
        return a, b + outward * min(shift, room, cap)
    def side_line(name, start, stop, inward, span, percentile=90):
        samples = edge(name, start, stop)
        line = fit(*samples, inward, percentile)
        return line, (lambda label: guard(line, samples, inward, name, *span, label))
    left, guard_left = side_line('left', my + .08 * H, my + .92 * H, 1, (my, my + H))
    right, guard_right = side_line('right', my + .08 * H, my + .92 * H, -1, (my, my + H))
    info, flags = {}, []
    if spread:
        outline_quad = np.float32(quad) / scale
        whole_top = fit(*edge('top', mx + .04 * W, mx + .96 * W), 1)
        whole_bottom = fit(*edge('bottom', mx + .04 * W, mx + .96 * W), -1, 50)
        if max(clipped, default=0) > .2 and margin < .2:      # before the costly crease search
            return refine(small, full, mask, quad, spread, margin + .07)
        if None not in (whole_top, whole_bottom, left, right):
            corners = [meet(whole_top, left), meet(whole_top, right),
                       meet(whole_bottom, right), meet(whole_bottom, left)]
            outline_quad = cv2.perspectiveTransform(np.float32(corners).reshape(-1, 1, 2),
                                                    np.linalg.inv(to_frame))[:, 0]
        top, bottom, info, flags = crease(full, outline_quad)
        ends = cv2.perspectiveTransform(np.float32([top, bottom]).reshape(-1, 1, 2), to_frame)[:, 0]
        slope = (ends[1][0] - ends[0][0]) / (ends[1][1] - ends[0][1])
        offset = ends[0][0] - slope * ends[0][1]
        overlap = .0015 * W
        xs = [slope * y + offset for y in (my, my + H)]
        spans = [('L', mx + .04 * W, min(xs) - .07 * W, guard_left('L-left'), (slope, offset + overlap)),
                 ('R', max(xs) + .07 * W, mx + .96 * W, (slope, offset - overlap), guard_right('R-right'))]
    else:
        spans = [('P', mx + .06 * W, mx + .94 * W, guard_left('P-left'), guard_right('P-right'))]
    inverse = np.linalg.inv(matrix)
    pages = []
    for side, start, stop, outer_left, outer_right in spans:
        _, guard_top = side_line('top', start, stop, 1, (start, stop))
        _, guard_bottom = side_line('bottom', start, stop, -1, (start, stop), 50)
        top, bottom = guard_top(f'{side}-top'), guard_bottom(f'{side}-bottom')
        if None in (top, bottom, outer_left, outer_right):
            return None, info, flags
        corners = [meet(top, outer_left), meet(top, outer_right),
                   meet(bottom, outer_right), meet(bottom, outer_left)]
        points = cv2.perspectiveTransform(np.float32(corners).reshape(-1, 1, 2), inverse)[:, 0]
        if not cv2.isContourConvex(points) or cv2.contourArea(points) < .25 * cv2.contourArea(np.float32(quad)):
            return None, info, flags
        pages.append((side, points))
    if max(clipped, default=0) > .2 and margin < .2:
        return refine(small, full, mask, quad, spread, margin + .07)
    if widened:
        info = dict(info, margin_ink=widened)
    if capped:
        flags = flags + ['margin-ink']
    return pages, info, flags


def guess(image, layout):
    """Draft per-page quads (normalized) for one photo: [(side, quad)], method, info, flags."""
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
    info, flags = {}, []
    if mask is not None:
        try:
            pages, info, flags = refine(small, full, mask, quad, spread)
        except (ValueError, IndexError, ZeroDivisionError, np.linalg.LinAlgError, cv2.error):
            pages = None
        if pages:
            cut = frame_cut(mask, pages)
            return ([(s, q / norm, ['touches-frame'] if s in cut else []) for s, q in pages],
                    'edges+gutter' if spread else 'edges', info, flags)
    flags = flags + [method]
    if spread:
        tl, tr, br, bl = quad
        top, bottom = (tl + tr) / 2, (bl + br) / 2
        pages = [('L', np.array([tl, top, bottom, bl])), ('R', np.array([top, tr, br, bottom]))]
    else:
        pages = [('P', quad)]
    return [(s, q / norm, []) for s, q in pages], method, info, flags


def frame_cut(mask, pages):
    """Sides of pages whose paper runs into the photo border (part of the page may be outside)."""
    import numpy as np
    h, w = mask.shape
    paper = mask > 127
    borders = [(paper[0], 0, 0), (paper[-1], 0, 1), (paper[:, 0], 1, 0), (paper[:, -1], 1, 1)]
    cut = set()
    for side, quad in pages:
        quad = np.asarray(quad, float)
        for line, axis, far in borders:
            # top/bottom borders: the page's column range; left/right: its row range
            values = quad[:, 0] if axis == 0 else quad[:, 1]
            a, b = int(max(values.min(), 0)), int(min(values.max(), len(line) - 1))
            if b - a < 10:
                continue
            near = quad[:, 1] / h if axis == 0 else quad[:, 0] / w
            distance = (1 - near.max()) if far else near.min()
            if line[a:b].mean() > .03 and distance < .15:
                cut.add(side)
    return cut
def physical_aspect(page, size):
    """Width/height of a quad from its side lengths in pixels."""
    import numpy as np
    q = np.float64(page['quad']) * size
    width = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    height = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    return width / max(height, 1)


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
    pages, previews, seen, sizes = [], [], {}, {}
    for number, path in enumerate(sources, 1):
        image = photo(path)
        sizes[str(path.resolve())] = image.size
        choices, method, info, flags = guess(image, args.layout)
        checksum = digest(path)
        image.thumbnail((1100, 1100))
        draw = ImageDraw.Draw(image)
        for side, points, extra in choices:
            item = dict(id=f'{number:04d}-{side}', source=str(path.resolve()), sha256=checksum,
                        quad=np.round(points, 5).tolist(), aspect=None, bleed_px=0, enhance=True,
                        include=True, reviewed=False, detection=method, flags=list(flags) + extra, **info)
            if checksum in seen:
                item['duplicate_of'] = seen[checksum]
                item['flags'].append('duplicate')
            pages.append(item)
            outline = [tuple(p * image.size) for p in points]
            colour = {'L': (255, 40, 20), 'R': (0, 170, 0)}.get(side, (230, 0, 230))
            draw.line(outline + outline[:1], fill=colour, width=3)
            draw.text(outline[0], item['id'] + (' CHECK' if item['flags'] else ''), fill=colour)
        seen[checksum] = str(path.resolve())
        preview = work / f'source-{number:03d}.jpg'
        image.save(preview, quality=92)
        previews.append(preview)
    for page in pages:
        q = np.float64(page['quad'])
        if ((q < .003).any() or (q > .997).any()) and 'touches-frame' not in page['flags']:
            page['flags'].append('touches-frame')
    sides = {p['id']: p for p in pages}
    for page in pages:
        partner = sides.get(page['id'][:-1] + 'R') if page['id'].endswith('-L') else None
        if partner is None:
            continue
        size = np.float64(sizes[page['source']])
        a, b = np.float64(page['quad']) * size, np.float64(partner['quad']) * size
        def taper(q):
            return np.linalg.norm(q[1] - q[0]) / np.linalg.norm(q[2] - q[3])
        def width(q):
            return np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])
        if abs(np.log(taper(a) / taper(b))) > .1 or abs(np.log(width(a) / width(b))) > .12:
            page['flags'].append('partner-mismatch')
            partner['flags'].append('partner-mismatch')
    for kind in ('P', 'LR'):
        group = [p for p in pages if p['id'][-1] in kind]
        if len(group) >= 5:
            aspects = np.array([physical_aspect(p, sizes[p['source']]) for p in group])
            middle = np.median(aspects)
            for page, value in zip(group, aspects):
                if abs(value / middle - 1) > .08:
                    page['flags'].append('aspect-outlier')
    dump(work / 'manifest.json', dict(version=1, pages=pages))
    contact(previews, work, 'sources')
    flagged = [p['id'] for p in pages if p['flags']]
    print(f'{len(sources)} photographs; {len(pages)} draft pages; {len(flagged)} flagged for a close check'
          + (f": {', '.join(flagged)}" if flagged else '') + '. Review every outline anyway.')


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
    rows = edge_metrics(paths)
    dump(work / 'edges.json', rows)
    suspects = []
    for row in rows:
        notes = [f'{k} dark {v} %' for k, v in row['dark'].items() if v > args.dark_limit]
        notes += [f'{k} ink {v} %' for k, v in row['ink'].items() if v > args.ink_limit]
        if notes:
            suspects.append(f"  page {row['page']}: " + ', '.join(notes))
    print(f'{len(paths)} pages rendered; inspect ALL sheets and individual edge exceptions: {work}')
    if suspects:
        print(f'{len(suspects)} pages with background or writing at a border (compare them with the photos):')
        print('\n'.join(suspects))


def oriented_size(path):
    """(width, height) after EXIF orientation, without decoding pixels."""
    from PIL import Image
    with Image.open(path) as image:
        width, height = image.size
        orientation = image.getexif().get(274, 1)
    return (height, width) if orientation in (5, 6, 7, 8) else (width, height)


def evaluate(args):
    """Compare a manifest with a verified reference manifest of the same photographs."""
    import numpy as np
    if args.json:
        target = args.json.resolve()
        if target in (args.manifest.resolve(), args.reference.resolve()):
            fail('--json must not be one of the input manifests.')
        if target.exists() and not args.replace:
            fail(f'Output exists: {target}; use a fresh path or --replace.')
    result, reference = load(args.manifest), load(args.reference)
    def key(page):
        return Path(page['source']).name, page['id'].rsplit('-', 1)[-1]
    expected = {key(p): p for p in reference['pages']}
    found = {key(p): p for p in result['pages']}
    sizes, rows = {}, []
    for k in sorted(set(expected) & set(found)):
        ref, got = expected[k], found[k]
        source = ref['source'] if Path(ref['source']).exists() else got['source']
        if source not in sizes:
            sizes[source] = np.float64(oriented_size(source))
        size = sizes[source]
        a, b = np.float64(ref['quad']) * size, np.float64(got['quad']) * size
        width = (np.linalg.norm(a[1] - a[0]) + np.linalg.norm(a[2] - a[3])) / 2
        error = np.linalg.norm(a - b, axis=1) / width * 100
        side = k[1]
        gutter = {'L': [1, 2], 'R': [0, 3]}.get(side, [])
        outer = [i for i in range(4) if i not in gutter]
        flagged = bool(got.get('flags')) or got.get('detection') in ('contour', 'full-frame-fallback')
        worst = float(error.max())
        rows.append(dict(id=got['id'], photo=k[0], side=side, flagged=flagged, flags=got.get('flags', []),
                         outer=round(float(error[outer].max()), 2),
                         gutter=round(float(error[gutter].max()), 2) if gutter else None,
                         worst=round(worst, 2), silent=not flagged and worst > args.tolerance))
    if not rows:
        fail('No common pages: match is by photo file name and page side (L, R, P).')
    outer = np.array([r['outer'] for r in rows])
    gutter = np.array([r['gutter'] for r in rows if r['gutter'] is not None])
    silent = [r for r in rows if r['silent']]
    flagged = [r for r in rows if r['flagged']]
    summary = dict(pages=len(rows), missing=sorted('-'.join(k) for k in set(expected) - set(found)),
                   extra=sorted('-'.join(k) for k in set(found) - set(expected)),
                   outer_median=round(float(np.median(outer)), 2), outer_p90=round(float(np.percentile(outer, 90)), 2),
                   gutter_median=round(float(np.median(gutter)), 2) if gutter.size else None,
                   gutter_p90=round(float(np.percentile(gutter, 90)), 2) if gutter.size else None,
                   flagged=len(flagged), silent=len(silent), tolerance=args.tolerance)
    print(f"{summary['pages']} pages compared; corner error, % of page width: outer median "
          f"{summary['outer_median']} / p90 {summary['outer_p90']}"
          + (f"; gutter median {summary['gutter_median']} / p90 {summary['gutter_p90']}" if gutter.size else ''))
    print(f"flagged for a check: {len(flagged)} ({100 * len(flagged) / len(rows):.0f} %); "
          f"silent failures (unflagged, error > {args.tolerance} %): {len(silent)}")
    for r in sorted(silent, key=lambda r: -r['worst']):
        print(f"  SILENT {r['id']} ({r['photo']}): worst corner {r['worst']} %")
    if summary['missing'] or summary['extra']:
        print(f"page set differs: missing {summary['missing']}, extra {summary['extra']}")
    if args.json:
        dump(args.json, dict(summary=summary, pages=rows))
    if len(silent) > args.max_silent:
        fail(f'{len(silent)} silent failures exceed --max-silent {args.max_silent}.')
    print('EVAL OK')


def grid_period(profile, low, high):
    """Ruling period (px) between `low` and `high` from the autocorrelation of a profile.

    The first autocorrelation peak that reaches 60 % of the strongest one is the cell; harmonics
    of thin lines do not create such a peak at half the period. The estimate is refined on the
    2nd-4th multiples. Returns (period, correlation at that lag), or (None, 0).
    """
    import numpy as np
    profile = np.asarray(profile, float)
    if len(profile) < 62:
        return None, 0.
    profile = profile - np.convolve(profile, np.ones(31) / 31, mode='same')
    profile = profile[15:-15] - profile[15:-15].mean()
    if len(profile) < 3 * low or not profile.any():
        return None, 0.
    ac = np.correlate(profile, profile, 'full')[len(profile) - 1:]
    ac = ac / ac[0]
    def peak(i):
        return 0 < i < len(ac) - 1 and ac[i] > ac[i - 1] and ac[i] >= ac[i + 1]
    def vertex(i):
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        return i + (.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c else 0.)
    peaks = [i for i in range(max(2, int(low)), min(int(high) + 1, len(ac) - 1)) if peak(i) and ac[i] > 0]
    if not peaks:
        return None, 0.
    strongest = max(ac[i] for i in peaks)
    first = next(i for i in peaks if ac[i] >= .6 * strongest)
    period = vertex(first)
    for multiple in (2, 3, 4):
        guess = int(round(multiple * period))
        near = [i for i in range(guess - 2, guess + 3) if peak(i)]
        if not near:
            break
        period = vertex(max(near, key=lambda i: ac[i])) / multiple
    return float(period), float(ac[first])


def page_images(path, work):
    """Page images from a directory of JPEG/PNG files or from a PDF rendered with pdftoppm."""
    path = Path(path)
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
    executable = shutil.which('pdftoppm')
    if not executable:
        fail('A PDF input needs Poppler pdftoppm; or pass the build *-pages directory.')
    render = work / 'measure-render'
    render.mkdir(exist_ok=True)
    for old in render.glob('page-*.png'):
        old.unlink()
    subprocess.run([executable, '-r', '200', '-png', str(path.resolve()), str(render / 'page')], check=True)
    return sorted(render.glob('page-*.png'))


def measure(args):
    """Physical page size from square grid cells (or ruled line spacing) on rectified pages."""
    import numpy as np
    from PIL import Image
    if not (math.isfinite(args.cell_mm) and args.cell_mm > 0) or not 0 < args.min_cell_px < args.max_cell_px:
        fail('--cell-mm must be positive and --min-cell-px < --max-cell-px.')
    work = work_dir(args.work, args.replace)
    rows = []
    for number, path in enumerate(page_images(args.input, work), 1):
        with Image.open(path) as image:
            red = np.asarray(image.convert('RGB'))[..., 0].astype('float32')
        h, w = red.shape
        core = red[int(.1 * h):int(.9 * h), int(.1 * w):int(.9 * w)]
        low, high = args.min_cell_px, args.max_cell_px
        px, sx = grid_period(core.mean(0), low, high)
        py, sy = grid_period(core.mean(1), low, high)
        row = dict(page=number, file=path.name, pixels=[w, h], cell_x=px and round(px, 2), cell_y=py and round(py, 2),
                   correlation=[round(sx, 2), round(sy, 2)])
        if py and sy >= args.min_correlation:
            row['height_mm'] = round(h / py * args.cell_mm, 1)
            if px and sx >= args.min_correlation and .9 < px / py < 1.1:
                row['width_mm'] = round(w / px * args.cell_mm, 1)
                row['aspect'] = round((w / px) / (h / py), 4)
                row['cell_ratio'] = round(px / py, 4)
        rows.append(row)
    if not rows:
        fail('No page images found.')
    usable = [r for r in rows if 'aspect' in r]
    lines = [r for r in rows if 'height_mm' in r]
    summary = dict(pages=len(rows), grid_pages=len(usable), ruled_pages=len(lines), cell_mm=args.cell_mm)
    if usable:
        for name in ('width_mm', 'height_mm', 'aspect', 'cell_ratio'):
            values = np.array([r[name] for r in usable])
            summary[name] = round(float(np.median(values)), 4 if name in ('aspect', 'cell_ratio') else 1)
            summary[name + '_p10_p90'] = [round(float(v), 4) for v in np.percentile(values, [10, 90])]
        print(f"{len(usable)}/{len(rows)} pages show a square grid of {args.cell_mm} mm: page "
              f"{summary['width_mm']} x {summary['height_mm']} mm, aspect {summary['aspect']} "
              f"(p10-p90 {summary['aspect_p10_p90'][0]}-{summary['aspect_p10_p90'][1]}), "
              f"cell x/y {summary['cell_ratio']}.")
        print(f"Suggested: \"aspect\": {summary['aspect']} for these pages and build --width-mm {summary['width_mm']}.")
    elif lines:
        summary['height_mm'] = round(float(np.median([r['height_mm'] for r in lines])), 1)
        print(f"Only horizontal ruling found on {len(lines)} pages: page height about {summary['height_mm']} mm "
              f"for {args.cell_mm} mm spacing. Width needs a known format or a grid.")
    else:
        print('No regular ruling found; set aspect from a known paper format instead.')
    dump(work / 'measure.json', dict(summary=summary, pages=rows))
    print(f'Details: {work / "measure.json"}')


def compare(args):
    """Each built page re-warped from its photo with a margin, the crop drawn: what lies outside it."""
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw
    record = load(args.record)
    wanted = None
    if args.pages:
        wanted = set()
        for part in args.pages.split(','):
            a, _, b = part.partition('-')
            if not a.strip().isdecimal() or (b and not b.strip().isdecimal()) or int(b or a) < int(a):
                fail(f'Invalid page range: {part}')
            wanted.update(range(int(a), int(b or a) + 1))
    work = work_dir(args.work, args.replace)
    done, last, source = 0, None, None
    for page in record['pages']:
        if wanted is not None and page['page'] not in wanted:
            continue
        if page['source'] != last:
            source, last = np.array(photo(page['source'])), page['source']
        if digest(page['source']) != page['sha256']:
            fail(f"Photograph changed since build: {page['source']}")
        w, h = page['pixels']
        mx, my = round(args.margin * w), round(args.margin * h)
        shift = np.array([[1, 0, mx], [0, 1, my], [0, 0, 1]], float) @ np.array(page['matrix'])
        view = cv2.warpPerspective(source, shift, (w + 2 * mx, h + 2 * my), flags=cv2.INTER_CUBIC,
                                   borderValue=(60, 60, 60))
        image = Image.fromarray(view)
        draw = ImageDraw.Draw(image)
        draw.rectangle([mx - 1, my - 1, mx + w, my + h], outline=(255, 0, 40), width=2)
        image.thumbnail((args.max_edge, args.max_edge))
        image.save(work / f"compare-{page['page']:04d}.jpg", quality=90)
        done += 1
    if not done:
        fail('No pages selected.')
    print(f'{done} pages -> {work}; red frame = PDF page border, outside it = what the crop left out.')


def edge_metrics(paths):
    """Per rendered page: dark (non-paper) share of a 0.6 % border strip and ink share of a 0.4 % strip."""
    import numpy as np
    from PIL import Image
    import cv2
    rows = []
    for number, path in enumerate(paths, 1):
        with Image.open(path) as image:
            rgb = np.asarray(image.convert('RGB'))
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
        r, b = rgb[..., 0].astype(int), rgb[..., 2].astype(int)
        dark = hsv[..., 2] < 120
        ink = ((b - r > 40) & (hsv[..., 1] > 80) & (hsv[..., 2] < 210)) | ((r - b > 50) & (hsv[..., 1] > 80))
        h, w = dark.shape
        row = dict(page=number)
        for name, wide in (('dark', .006), ('ink', .004)):
            mask = dark if name == 'dark' else ink
            sw, sh = max(2, round(wide * w)), max(2, round(wide * h))
            strips = dict(top=mask[:sh], bottom=mask[-sh:], left=mask[:, :sw], right=mask[:, -sw:])
            row[name] = {k: round(float(v.mean()) * 100, 2) for k, v in strips.items()}
        rows.append(row)
    return rows


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
    p = subs.add_parser('eval', help='Compare a manifest with a verified reference manifest')
    p.add_argument('manifest', type=Path)
    p.add_argument('--reference', type=Path, required=True)
    p.add_argument('--tolerance', type=float, default=3.0, help='Corner error, %% of page width, that counts as a failure')
    p.add_argument('--max-silent', type=int, default=0, help='Allowed unflagged failures before exit 1')
    p.add_argument('--json', type=Path, help='Write per-page results')
    p.set_defaults(run=evaluate)
    p = subs.add_parser('measure', help='Page size and aspect from grid cells on built pages')
    p.add_argument('input', type=Path, help='Build *-pages directory or a PDF')
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--cell-mm', type=float, default=5.0)
    p.add_argument('--min-cell-px', type=float, default=10.0)
    p.add_argument('--max-cell-px', type=float, default=150.0)
    p.add_argument('--min-correlation', type=float, default=.3, help='Ruling autocorrelation needed to trust a page')
    p.set_defaults(run=measure)
    p = subs.add_parser('compare', help='Built pages re-warped from the photos with a margin around the crop')
    p.add_argument('record', type=Path, help='The .scan.json written by build')
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--pages', help='For example 3,7-9; default all')
    p.add_argument('--margin', type=float, default=.04, help='Margin as a share of the page size')
    p.add_argument('--max-edge', type=positive, default=1400)
    p.set_defaults(run=compare)
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
    p.add_argument('--dark-limit', type=float, default=10.0, help='%% dark pixels in a border strip worth a look')
    p.add_argument('--ink-limit', type=float, default=1.0, help='%% ink pixels in a border strip worth a look')
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
