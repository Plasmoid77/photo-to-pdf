# Просмотр страниц недорогими моделями

Задание для проверяющего агента (на английском: так его одинаково понимают
разные модели). `<list>` — файл со списком путей к изображениям `compare`, не
больше 16 на агента. Подставь путь и передай текст без изменений.

```text
You screen page crops of a scanned notebook or document. The file <list> lists
JPG paths named compare-NNNN.jpg (NNNN = page number). Read each image once.
Each image is one page re-cut from the original photo with a 4 % margin around
it; the thin RED rectangle is the final crop. Everything outside it is cut away.

For every image look at the four sides in turn: top, right, bottom, left.
A side is a DEFECT if any of these holds:
  cut      - a stroke of writing or drawing (letter, digit, date, line, arrow,
             dimension) crosses the red line or lies between the red line and
             the paper edge outside it;
  strip    - inside the red line there is a band of non-paper (desk, dark
             background, screen, fold shadow) thicker than about two letter
             strokes along a noticeable part of the side;
  neighbour- part of the facing page or the fold of a spread is inside the red
             line.
NOT defects: a hand, finger or thumb holding the paper (report it as "hand");
writing that runs off the physical edge of the paper itself; a hairline of
shadow; slight curvature of the paper edge.

Reply ONLY with one line per image, no other text:
  NNNN | ok
  NNNN | hand
  NNNN | defect | <side> | <cut|strip|neighbour> | <what, at most 8 words>
Several defects: separate lines. Do not spawn agents. Do not write files.
```

Ответы двух разных моделей объединяются: страница идёт на проверку основному
агенту, если хотя бы одна модель указала `defect`.
