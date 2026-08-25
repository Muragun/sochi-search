#!/usr/bin/env python3
"""
Подготовка изображения страницы к OCR.

Архивные PDF sochi.ru — это чаще всего многократные ксерокопии: низкий
контраст, неравномерная подсветка, просвет обратной стороны, перекос подачи.
Внутренняя бинаризация Tesseract (глобальный Оцу) на таком листе выдаёт
пустую страницу. Здесь используется локальный порог Сауволы, который считает
порог по окрестности каждого пикселя и поэтому переживает градиент фона.

Модуль намеренно зависит только от numpy и Pillow: на сервере не должно
появляться OpenCV ради одной функции.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter


@dataclass(frozen=True)
class PreparedPage:
    image: Image.Image
    skew_degrees: float
    ink_ratio: float
    blank: bool
    method: str


def _box_statistics(
    values: np.ndarray,
    radius: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Локальные среднее и СКО по квадратному окну (2r+1)."""

    padded = np.pad(
        values.astype(np.float64),
        radius,
        mode="edge",
    )

    integral = np.zeros(
        (padded.shape[0] + 1, padded.shape[1] + 1),
        dtype=np.float64,
    )
    integral_squared = np.zeros_like(integral)

    np.cumsum(np.cumsum(padded, axis=0), axis=1, out=integral[1:, 1:])
    np.cumsum(
        np.cumsum(padded * padded, axis=0),
        axis=1,
        out=integral_squared[1:, 1:],
    )

    size = 2 * radius + 1
    area = float(size * size)

    def window(source: np.ndarray) -> np.ndarray:
        return (
            source[size:, size:]
            - source[:-size, size:]
            - source[size:, :-size]
            + source[:-size, :-size]
        )

    mean = window(integral) / area
    mean_squared = window(integral_squared) / area
    variance = np.maximum(mean_squared - mean * mean, 0.0)

    return mean, np.sqrt(variance)


def sauvola_binarize(
    array: np.ndarray,
    *,
    window: int | None = None,
    k: float = 0.15,
    dynamic_range: float = 128.0,
    statistics_scale: int = 4,
) -> np.ndarray:
    """
    Возвращает изображение, где 0 — чернила, 255 — фон.

    Порог Сауволы считается на уменьшенной копии и растягивается обратно
    билинейно. Карта порогов гладкая, поэтому результат практически совпадает
    с полным расчётом, а стоимость падает примерно в `statistics_scale`^2 раз.
    """

    height, width = array.shape

    if window is None:
        window = max(15, (width // 25) | 1)

    scale = max(1, int(statistics_scale))

    if scale > 1 and min(height, width) >= 4 * scale:
        small = np.asarray(
            Image.fromarray(array).resize(
                (max(1, width // scale), max(1, height // scale)),
                Image.BILINEAR,
            )
        )
        radius = max(2, (window // scale) // 2)
    else:
        small = array
        radius = max(3, window // 2)

    mean, deviation = _box_statistics(small, radius)

    threshold = mean * (
        1.0 + k * (deviation / dynamic_range - 1.0)
    )

    if threshold.shape != array.shape:
        threshold = np.asarray(
            Image.fromarray(
                np.clip(threshold, 0, 255).astype(np.float32)
            ).resize((width, height), Image.BILINEAR),
            dtype=np.float32,
        )

    ink = array < threshold

    return np.where(ink, 0, 255).astype(np.uint8)


def _projection_sharpness(
    ink_rows: np.ndarray,
    ink_columns: np.ndarray,
    *,
    height: int,
    width: int,
    angle_degrees: float,
) -> float:
    """
    Резкость горизонтальной проекции при заданном наклоне.

    Строки текста дают чередование «пусто/густо» по вертикали. Наклоняем
    координату строки в зависимости от колонки и смотрим, насколько
    контрастным становится профиль. Максимум приходится на угол, при котором
    строки строго горизонтальны.
    """

    tangent = math.tan(math.radians(angle_degrees))

    shifted = ink_rows - (ink_columns - width / 2.0) * tangent
    indices = np.rint(shifted).astype(np.int64)
    np.clip(indices, 0, height - 1, out=indices)

    profile = np.bincount(indices, minlength=height).astype(np.float64)

    return float(np.var(profile))


def estimate_skew(
    array: np.ndarray,
    *,
    limit_degrees: float = 4.0,
    coarse_step: float = 0.5,
    fine_step: float = 0.1,
) -> float:
    """Определяет перекос строк по дисперсии горизонтальной проекции."""

    height, width = array.shape
    step = max(1, min(height // 800, width // 800, 6))
    small = array[::step, ::step]

    small_height, small_width = small.shape
    threshold = np.percentile(small, 20)
    rows, columns = np.nonzero(small < threshold)

    if rows.size < 200:
        return 0.0

    # Дальше работаем с координатами чернил: это на порядок меньше данных,
    # чем полная матрица, и поиск угла становится дешёвым.
    if rows.size > 300_000:
        selection = np.linspace(
            0,
            rows.size - 1,
            300_000,
        ).astype(np.int64)
        rows = rows[selection]
        columns = columns[selection]

    ink_rows = rows.astype(np.float64)
    ink_columns = columns.astype(np.float64)

    def best_within(center: float, span: float, increment: float) -> float:
        angles = np.arange(
            center - span,
            center + span + increment / 2.0,
            increment,
        )

        best_angle = center
        best_score = -1.0

        for angle in angles:
            if abs(angle) > limit_degrees + 1e-9:
                continue

            score = _projection_sharpness(
                ink_rows,
                ink_columns,
                height=small_height,
                width=small_width,
                angle_degrees=float(angle),
            )

            if score > best_score:
                best_score = score
                best_angle = float(angle)

        return best_angle

    coarse = best_within(0.0, limit_degrees, coarse_step)
    return round(best_within(coarse, coarse_step, fine_step), 2)


def prepare_page(
    image: Image.Image,
    *,
    deskew: bool = True,
    binarize: bool = True,
    despeckle: bool = True,
    blank_ink_ratio: float = 0.0004,
    sauvola_k: float = 0.15,
) -> PreparedPage:
    """
    Готовит страницу к распознаванию.

    `sauvola_k` — насколько порог отходит от локального среднего. Чем
    больше, тем строже отбор чернил. Замер на выцветшей копии показал, что
    одного значения на все страницы не существует: при k = 0,15 хорошо
    читается обычная архивная копия (98,4 % против 93,9 %), при k = 0,10 —
    сильно засвеченная (54,6 % против 49,7 %). Поэтому основное значение
    берётся строгое, а мягкое остаётся второй ступенью для страниц, на
    которых Tesseract не уверен в себе.
    """

    grayscale = image.convert("L")
    array = np.asarray(grayscale)

    if not binarize:
        skew = estimate_skew(array) if deskew else 0.0

        if abs(skew) >= 0.2:
            grayscale = grayscale.rotate(
                skew,
                resample=Image.BICUBIC,
                fillcolor=255,
                expand=False,
            )
            array = np.asarray(grayscale)

        ink_ratio = float((array < 128).mean())

        return PreparedPage(
            image=grayscale,
            skew_degrees=skew,
            ink_ratio=round(ink_ratio, 5),
            blank=ink_ratio < blank_ink_ratio,
            method="grayscale",
        )

    # Сначала бинаризация: она снимает градиент подсветки и тёмную кромку
    # листа. Если оценивать перекос до неё, эти артефакты дают более сильную
    # проекцию, чем сами строки текста, и угол определяется неверно.
    binary = sauvola_binarize(array, k=sauvola_k)
    result = Image.fromarray(binary)

    if despeckle:
        # Медиана 3x3 убирает одиночные точки тонера, не трогая штрихи.
        #
        # Замер на выцветшей копии: без неё двенадцать из восемнадцати
        # сочетаний параметров бинаризации дают ровно ноль символов —
        # анализ раскладки принимает точки тонера за элементы вёрстки и
        # не находит ни одной строки. Это не косметика, а условие того,
        # что страница вообще будет прочитана.
        result = result.filter(ImageFilter.MedianFilter(3))
        binary = np.asarray(result)

    skew = estimate_skew(binary) if deskew else 0.0

    if abs(skew) >= 0.2:
        result = result.rotate(
            skew,
            resample=Image.BICUBIC,
            fillcolor=255,
            expand=False,
        )
        binary = (
            np.asarray(result) > 128
        ).astype(np.uint8) * 255
        result = Image.fromarray(binary)

    ink_ratio = float((binary == 0).mean())

    return PreparedPage(
        image=result,
        skew_degrees=skew,
        ink_ratio=round(ink_ratio, 5),
        blank=ink_ratio < blank_ink_ratio,
        method="sauvola",
    )
