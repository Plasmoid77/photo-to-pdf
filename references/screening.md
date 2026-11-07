# Просмотр страниц недорогими моделями

Необязательная схема для большого объёма. Полосы фона и обрезанную запись у
внешних краёв уже перечисляют `review` и `compare`; проверяющие нужны, если хочется
второго взгляда на все страницы. Сделай `compare` для всех страниц и раздай
картинки пачками не больше 16 параллельным агентам на недорогой модели с
изображениями (в Claude Code — Sonnet) с заданием ниже без изменений. Сам
открывай только страницы, где проверяющий указал `defect`, и страницы с флагами
`prepare`; правь только подтверждённое. Номера страниц в ответах проверяющих
заодно проверяют порядок.

Замеры. На 32 страницах черновика с 8 известными ошибками угла > 3 % такая схема нашла все 8 (Sonnet — 7, флаги — оставшуюся) при 4 ложных тревогах; Haiku нашёл 4 и добавил только ложные тревоги, поэтому одного Haiku недостаточно. На второй тетради (32 страницы, задание не подгонялось) Sonnet и `gpt-5.6-luna` нашли видимые полосы фона без ложных тревог, но пропустили страницы с ошибкой угла 1,5–2 %; `gpt-6.1-terra` отметил почти всё, включая 12 из 19 чистых страниц, — экономии с ним нет. На всех 153 страницах второй тетради (прогон в Codex) luna не отметил ни одной страницы, пропустив ошибку угла 7 % (её поймал флаг), а terra отметил 37–43 страницы, в основном полоски 1–2 % у верхнего края и строки у сгиба, и уточнение задания этого не изменило. В Codex подходящей недорогой модели нет: обходись флагами `prepare` и списками `review` и `compare`. Задание составлено по страницам первой тетради.

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
