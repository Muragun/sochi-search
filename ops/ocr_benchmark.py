#!/usr/bin/env python3
"""
Замер качества распознавания на воспроизводимых страницах.

Доступа к рабочему серверу нет, а рассуждать об OCR без цифр бессмысленно:
любое изменение бинаризации или параметров Tesseract одинаково правдоподобно
звучит и в плюс, и в минус. Здесь страница синтезируется из известного текста,
затем портится так, как её портит многократное копирование и сканирование:
низкий контраст, неравномерная подсветка, просвет обратной стороны, перекос,
точки тонера, тёмная кромка от крышки сканера и артефакты JPEG.

Эталон известен побайтово, поэтому считаются CER и WER, а не «на глаз стало
лучше». Проверяется и то, ради чего всё делается: извлеклись ли вид акта,
номер и дата.

Запуск:

    python -m ops.ocr_benchmark
    python -m ops.ocr_benchmark --pages archival --repeat 3
    python -m ops.ocr_benchmark --json результат.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


DOCUMENT_LINES: tuple[str, ...] = (
    "АДМИНИСТРАЦИЯ ГОРОДА СОЧИ",
    "",
    "ПОСТАНОВЛЕНИЕ",
    "",
    "от 14 марта 2024 года № 512-р",
    "",
    "город Сочи",
    "",
    "О внесении изменений в постановление администрации",
    "города Сочи от 13 декабря 2013 года № 2739",
    "«Об утверждении муниципальной программы города Сочи",
    "«Благоустройство территории муниципального образования»",
    "",
    "В соответствии с Федеральным законом от 6 октября 2003 года",
    "№ 131-ФЗ «Об общих принципах организации местного",
    "самоуправления в Российской Федерации», Уставом города Сочи,",
    "в целях повышения эффективности расходования средств бюджета",
    "города Сочи постановляю:",
    "",
    "1. Внести в постановление администрации города Сочи",
    "от 13 декабря 2013 года № 2739 «Об утверждении муниципальной",
    "программы города Сочи «Благоустройство территории",
    "муниципального образования городской округ город-курорт Сочи",
    "Краснодарского края» следующие изменения:",
    "",
    "1.1. Паспорт муниципальной программы изложить в новой",
    "редакции согласно приложению к настоящему постановлению.",
    "",
    "1.2. Раздел 3 «Перечень основных мероприятий программы»",
    "дополнить пунктом 3.14 следующего содержания:",
    "«3.14. Содержание и ремонт объектов уличного освещения",
    "в границах городского округа город-курорт Сочи.».",
    "",
    "2. Департаменту городского хозяйства администрации города",
    "Сочи обеспечить размещение настоящего постановления",
    "на официальном сайте администрации города Сочи",
    "в информационно-телекоммуникационной сети «Интернет».",
    "",
    "3. Контроль за выполнением настоящего постановления",
    "возложить на заместителя Главы города Сочи.",
    "",
    "4. Настоящее постановление вступает в силу со дня",
    "его официального опубликования.",
    "",
    "Глава города Сочи",
)

SERIF_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
)

BOLD_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
)

WORD_SPLIT_RE = re.compile(r"\s+")


def resolve_font(candidates: Sequence[str]) -> str:
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate

    raise RuntimeError(
        "Не найден ни один шрифт с кириллицей: "
        + ", ".join(candidates)
    )


@dataclass(frozen=True)
class PageProfile:
    """Как именно портится идеальная страница."""

    name: str
    title: str

    render_dpi: int = 200

    # Уровни серого: чернила и фон. У свежего скана разница около 200
    # уровней, у пятой копии — несколько десятков.
    ink_level: int = 15
    background_level: int = 250

    blur_radius: float = 0.0
    skew_degrees: float = 0.0

    illumination_strength: float = 0.0
    bleed_through: float = 0.0
    edge_shadow: float = 0.0

    gaussian_noise: float = 0.0
    speckle_ratio: float = 0.0

    jpeg_quality: int = 0


PAGE_PROFILES: tuple[PageProfile, ...] = (
    PageProfile(
        name="modern",
        title="Современный скан",
        render_dpi=300,
        ink_level=20,
        background_level=248,
        blur_radius=0.4,
        skew_degrees=0.2,
        gaussian_noise=3.0,
        jpeg_quality=88,
    ),
    PageProfile(
        name="archival",
        title="Архивная ксерокопия",
        render_dpi=200,
        ink_level=95,
        background_level=205,
        blur_radius=1.1,
        skew_degrees=1.9,
        illumination_strength=0.42,
        bleed_through=0.16,
        edge_shadow=0.55,
        gaussian_noise=9.0,
        speckle_ratio=0.0016,
        jpeg_quality=52,
    ),
    PageProfile(
        name="faded",
        title="Выцветшая копия с сильной засветкой",
        render_dpi=200,
        ink_level=120,
        background_level=196,
        blur_radius=1.5,
        skew_degrees=-2.6,
        illumination_strength=0.62,
        bleed_through=0.22,
        edge_shadow=0.75,
        gaussian_noise=12.0,
        speckle_ratio=0.0030,
        jpeg_quality=45,
    ),
    PageProfile(
        name="rotated",
        title="Архивная копия, повёрнутая на 90°",
        render_dpi=200,
        ink_level=95,
        background_level=205,
        blur_radius=1.1,
        skew_degrees=1.4,
        illumination_strength=0.40,
        bleed_through=0.14,
        edge_shadow=0.50,
        gaussian_noise=9.0,
        speckle_ratio=0.0016,
        jpeg_quality=52,
    ),
)

PROFILES_BY_NAME = {
    profile.name: profile
    for profile in PAGE_PROFILES
}


def ground_truth_text() -> str:
    return " ".join(
        line
        for line in DOCUMENT_LINES
        if line
    )


def render_clean_page(
    profile: PageProfile,
) -> "Any":
    """Рисует идеальную страницу А4 в оттенках серого."""

    from PIL import Image, ImageDraw, ImageFont

    dpi = profile.render_dpi
    width = int(8.27 * dpi)
    height = int(11.69 * dpi)

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)

    body_size = max(10, int(dpi * 0.155))
    heading_size = max(12, int(dpi * 0.185))

    body_font = ImageFont.truetype(
        resolve_font(SERIF_FONT_CANDIDATES),
        body_size,
    )
    heading_font = ImageFont.truetype(
        resolve_font(BOLD_FONT_CANDIDATES),
        heading_size,
    )

    left_margin = int(dpi * 1.1)
    top = int(dpi * 0.9)
    line_height = int(body_size * 1.62)

    for index, line in enumerate(DOCUMENT_LINES):
        if not line:
            top += line_height // 2
            continue

        font = (
            heading_font
            if index <= 4
            else body_font
        )
        centered = index <= 6

        if centered:
            text_width = draw.textlength(line, font=font)
            x = (width - text_width) / 2.0
        else:
            x = float(left_margin)

        draw.text(
            (x, float(top)),
            line,
            font=font,
            fill=0,
        )
        top += line_height

    return image


def degrade_page(
    image: "Any",
    profile: PageProfile,
    *,
    seed: int,
) -> "Any":
    """Превращает идеальную страницу в правдоподобный скан."""

    import numpy as np
    from PIL import Image, ImageFilter

    generator = np.random.default_rng(seed)
    array = np.asarray(image).astype(np.float32)

    # Просвет обратной стороны: зеркальная копия текста бледным тоном.
    if profile.bleed_through > 0:
        reverse = np.fliplr(array)
        ink_mask = (255.0 - reverse) / 255.0
        array = array - ink_mask * 255.0 * profile.bleed_through

    # Сжатие динамического диапазона: именно это ломает глобальный порог Оцу.
    span = profile.background_level - profile.ink_level
    array = (
        profile.ink_level
        + (array / 255.0) * span
    )

    if profile.blur_radius > 0:
        array = np.asarray(
            Image.fromarray(
                np.clip(array, 0, 255).astype(np.uint8)
            ).filter(
                ImageFilter.GaussianBlur(profile.blur_radius)
            )
        ).astype(np.float32)

    height, width = array.shape

    # Неравномерная подсветка: лампа сканера светит ярче с одной стороны.
    if profile.illumination_strength > 0:
        rows = np.linspace(-1.0, 1.0, height, dtype=np.float32)
        columns = np.linspace(-1.0, 1.0, width, dtype=np.float32)
        grid_rows, grid_columns = np.meshgrid(
            rows,
            columns,
            indexing="ij",
        )

        radial = np.sqrt(
            (grid_rows + 0.35) ** 2
            + (grid_columns - 0.45) ** 2
        ) / math.sqrt(2.0)

        illumination = (
            1.0
            - profile.illumination_strength * radial
        )
        array = array * illumination

    # Тень от неплотно прижатой крышки: тёмная полоса вдоль края листа.
    if profile.edge_shadow > 0:
        band = max(8, int(width * 0.055))
        ramp = np.linspace(
            1.0 - profile.edge_shadow,
            1.0,
            band,
            dtype=np.float32,
        )
        array[:, :band] *= ramp
        array[:band, :] *= ramp[:, None]

    if profile.gaussian_noise > 0:
        array = array + generator.normal(
            0.0,
            profile.gaussian_noise,
            size=array.shape,
        ).astype(np.float32)

    # Точки тонера: именно они взрывают анализ раскладки Tesseract.
    if profile.speckle_ratio > 0:
        count = int(array.size * profile.speckle_ratio)
        rows = generator.integers(0, height, count)
        columns = generator.integers(0, width, count)
        array[rows, columns] = generator.integers(
            0,
            60,
            count,
        ).astype(np.float32)

    result = Image.fromarray(
        np.clip(array, 0, 255).astype(np.uint8)
    )

    if profile.skew_degrees:
        result = result.rotate(
            profile.skew_degrees,
            resample=Image.BICUBIC,
            fillcolor=int(profile.background_level),
            expand=False,
        )

    if profile.jpeg_quality:
        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False,
        ) as handle:
            jpeg_path = Path(handle.name)

        try:
            result.convert("L").save(
                jpeg_path,
                format="JPEG",
                quality=profile.jpeg_quality,
            )
            result = Image.open(jpeg_path).convert("L")
            result.load()
        finally:
            jpeg_path.unlink(missing_ok=True)

    if profile.name == "rotated":
        result = result.rotate(
            90,
            expand=True,
            fillcolor=int(profile.background_level),
        )

    return result


def build_scanned_pdf(
    image: "Any",
    profile: PageProfile,
    destination: Path,
) -> Path:
    """Заворачивает растр в PDF без текстового слоя — как настоящий скан."""

    import pymupdf

    image_path = destination.with_suffix(".jpg")
    image.save(
        image_path,
        format="JPEG",
        quality=max(60, profile.jpeg_quality or 85),
    )

    document = pymupdf.open()

    try:
        width_points = image.width * 72.0 / profile.render_dpi
        height_points = image.height * 72.0 / profile.render_dpi

        page = document.new_page(
            width=width_points,
            height=height_points,
        )
        page.insert_image(
            page.rect,
            filename=str(image_path),
        )
        document.save(str(destination), garbage=3, deflate=True)

    finally:
        document.close()
        image_path.unlink(missing_ok=True)

    return destination


def normalize_for_scoring(value: str) -> str:
    """Одинаково причёсывает эталон и распознанное перед сравнением."""

    text = WORD_SPLIT_RE.sub(" ", value or "").strip()

    # Кавычки и тире Tesseract передаёт как придётся, и спорить с ним
    # бессмысленно: на поиск вид кавычки не влияет.
    replacements = {
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "„": '"',
        "‘": "'",
        "’": "'",
        "—": "-",
        "–": "-",
        "‑": "-",
        " ": " ",
    }

    for source, target in replacements.items():
        text = text.replace(source, target)

    return text


def edit_distance(
    reference: Sequence[Any],
    hypothesis: Sequence[Any],
) -> int:
    """
    Расстояние Левенштейна между двумя последовательностями.

    Своё, а не из библиотеки: замерочный инструмент не должен добавлять
    зависимость в `requirements.txt`, которая на рабочем сервере не нужна
    ни разу. Совпадающие начало и конец отбрасываются заранее — на почти
    одинаковых строках это убирает почти всю работу.
    """

    first = list(reference)
    second = list(hypothesis)

    start = 0
    limit = min(len(first), len(second))

    while start < limit and first[start] == second[start]:
        start += 1

    first = first[start:]
    second = second[start:]

    end = 0
    limit = min(len(first), len(second))

    while end < limit and first[-1 - end] == second[-1 - end]:
        end += 1

    if end:
        first = first[:-end]
        second = second[:-end]

    if not first:
        return len(second)

    if not second:
        return len(first)

    # Короткая последовательность идёт в строку таблицы: память O(min).
    if len(first) < len(second):
        first, second = second, first

    previous = list(range(len(second) + 1))

    for outer_index, outer_item in enumerate(first, start=1):
        current = [outer_index]
        append = current.append

        for inner_index, inner_item in enumerate(second, start=1):
            append(
                min(
                    previous[inner_index] + 1,
                    current[inner_index - 1] + 1,
                    previous[inner_index - 1]
                    + (outer_item != inner_item),
                )
            )

        previous = current

    return previous[-1]


def character_error_rate(
    reference: str,
    hypothesis: str,
) -> float:
    if not reference:
        return 0.0 if not hypothesis else 1.0

    return min(
        1.0,
        edit_distance(reference, hypothesis) / len(reference),
    )


def word_error_rate(
    reference: str,
    hypothesis: str,
) -> float:
    reference_words = reference.split()
    hypothesis_words = hypothesis.split()

    if not reference_words:
        return 0.0 if not hypothesis_words else 1.0

    return min(
        1.0,
        edit_distance(reference_words, hypothesis_words)
        / len(reference_words),
    )


@dataclass
class PageMeasurement:
    profile: str
    title: str
    seconds: float
    characters: int
    cer: float
    wer: float
    accuracy: float
    cyrillic_ratio: float
    quality_score: int
    quality_reason: str
    readable: bool
    header_reliable: bool
    render_dpi: int
    megapixels: float
    skew_degrees: float
    text_sample: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "title": self.title,
            "seconds": round(self.seconds, 2),
            "characters": self.characters,
            "cer": round(self.cer, 4),
            "wer": round(self.wer, 4),
            "accuracy": round(self.accuracy, 4),
            "cyrillic_ratio": round(self.cyrillic_ratio, 4),
            "quality_score": self.quality_score,
            "quality_reason": self.quality_reason,
            "readable": self.readable,
            "header_reliable": self.header_reliable,
            "render_dpi": self.render_dpi,
            "megapixels": self.megapixels,
            "skew_degrees": self.skew_degrees,
            **self.extra,
        }


def measure_page(
    pdf_path: Path,
    profile: PageProfile,
    *,
    recognize: Callable[..., Any],
    working_directory: Path,
    keep_sample: bool,
) -> PageMeasurement:
    import pymupdf

    from crawler_v2.pdf_ocr import evaluate_text_quality, legal_header_is_reliable

    reference = normalize_for_scoring(ground_truth_text())

    document = pymupdf.open(str(pdf_path))

    try:
        page = document.load_page(0)

        started = time.monotonic()
        result = recognize(page, working_directory)
        seconds = time.monotonic() - started

    finally:
        document.close()

    recognized = normalize_for_scoring(
        getattr(result, "text", "") or ""
    )
    quality = evaluate_text_quality(recognized, minimum_length=80)

    cer = character_error_rate(reference, recognized)
    wer = word_error_rate(reference, recognized)

    return PageMeasurement(
        profile=profile.name,
        title=profile.title,
        seconds=seconds,
        characters=len(recognized),
        cer=cer,
        wer=wer,
        accuracy=1.0 - cer,
        cyrillic_ratio=quality.cyrillic_ratio,
        quality_score=quality.score,
        quality_reason=quality.reason,
        readable=quality.readable,
        header_reliable=legal_header_is_reliable(recognized),
        render_dpi=int(getattr(result, "render_dpi", 0) or 0),
        megapixels=float(getattr(result, "megapixels", 0.0) or 0.0),
        skew_degrees=float(getattr(result, "skew_degrees", 0.0) or 0.0),
        text_sample=recognized[:400] if keep_sample else "",
    )


def default_recognizer(
    **overrides: Any,
) -> Callable[..., Any]:
    """Текущий рабочий конвейер, с точечными подменами параметров."""

    from crawler_v2.config import settings
    from crawler_v2.pdf_ocr_engine import ocr_page, resolve_tesseract

    binary = resolve_tesseract(settings.pdf_ocr_binary)

    parameters: dict[str, Any] = {
        "tessdata": settings.pdf_ocr_tessdata_path,
        "languages": settings.pdf_ocr_languages,
        "minimum_dpi": settings.pdf_ocr_minimum_dpi,
        "maximum_dpi": settings.pdf_ocr_maximum_dpi,
        "pixel_budget": settings.pdf_ocr_pixel_budget,
        "psm": settings.pdf_ocr_psm,
        "oem": settings.pdf_ocr_oem,
        "threads": settings.pdf_ocr_threads,
        "timeout_seconds": settings.pdf_ocr_page_timeout_seconds,
        "deskew": settings.pdf_ocr_deskew,
        "binarize": settings.pdf_ocr_binarize,

        # Лестницу повторов замер обязан воспроизводить целиком: без неё
        # измеряется не тот конвейер, который работает на сервере.
        "sauvola_k": settings.pdf_ocr_sauvola_k,
        "confidence_floor": settings.pdf_ocr_confidence_floor,
        "retry_sauvola_k": settings.pdf_ocr_retry_sauvola_k,
        "retry_rotations": settings.pdf_ocr_retry_rotations,
        "ladder_budget_seconds": (
            settings.pdf_ocr_ladder_budget_seconds
        ),
    }
    parameters.update(overrides)

    def recognize(page: Any, working_directory: Path) -> Any:
        return ocr_page(
            page,
            binary=binary,
            working_directory=working_directory,
            **parameters,
        )

    return recognize


def print_table(measurements: Sequence[PageMeasurement]) -> None:
    header = (
        f"{'страница':<34} "
        f"{'время':>7} "
        f"{'симв.':>7} "
        f"{'точность':>9} "
        f"{'WER':>7} "
        f"{'кирилл.':>8} "
        f"{'балл':>5} "
        f"{'реквизиты':>10}"
    )
    print(header)
    print("-" * len(header))

    for item in measurements:
        print(
            f"{item.title[:34]:<34} "
            f"{item.seconds:>6.2f}с "
            f"{item.characters:>7} "
            f"{item.accuracy * 100:>8.1f}% "
            f"{item.wer:>7.3f} "
            f"{item.cyrillic_ratio:>8.3f} "
            f"{item.quality_score:>5} "
            f"{'да' if item.header_reliable else 'нет':>10}"
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Замер качества PDF OCR на синтетических страницах",
    )
    parser.add_argument(
        "--pages",
        default="all",
        help=(
            "Через запятую: "
            + ", ".join(PROFILES_BY_NAME)
            + " или all"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Сколько раз повторить каждую страницу",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20240314,
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Куда сложить измерения в JSON",
    )
    parser.add_argument(
        "--keep-pdf",
        type=Path,
        default=None,
        help="Каталог, в котором оставить сгенерированные PDF",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Показать начало распознанного текста",
    )
    return parser.parse_args()


def selected_profiles(value: str) -> list[PageProfile]:
    if value.strip().lower() in {"all", "все"}:
        return list(PAGE_PROFILES)

    result: list[PageProfile] = []

    for name in value.split(","):
        key = name.strip().lower()

        if not key:
            continue

        if key not in PROFILES_BY_NAME:
            raise SystemExit(
                f"Неизвестная страница: {key}. "
                f"Доступны: {', '.join(PROFILES_BY_NAME)}"
            )

        result.append(PROFILES_BY_NAME[key])

    return result or list(PAGE_PROFILES)


def main() -> int:
    arguments = parse_arguments()
    profiles = selected_profiles(arguments.pages)

    random.seed(arguments.seed)

    recognize = default_recognizer()
    measurements: list[PageMeasurement] = []

    with tempfile.TemporaryDirectory(prefix="ocr-benchmark-") as directory:
        working_directory = Path(directory)

        for profile in profiles:
            clean = render_clean_page(profile)

            for attempt in range(max(1, arguments.repeat)):
                degraded = degrade_page(
                    clean,
                    profile,
                    seed=arguments.seed + attempt,
                )

                pdf_path = working_directory / (
                    f"{profile.name}-{attempt}.pdf"
                )
                build_scanned_pdf(degraded, profile, pdf_path)

                if arguments.keep_pdf:
                    arguments.keep_pdf.mkdir(parents=True, exist_ok=True)
                    (
                        arguments.keep_pdf / pdf_path.name
                    ).write_bytes(pdf_path.read_bytes())

                measurements.append(
                    measure_page(
                        pdf_path,
                        profile,
                        recognize=recognize,
                        working_directory=working_directory,
                        keep_sample=arguments.sample,
                    )
                )

    print_table(measurements)

    if arguments.sample:
        print()

        for item in measurements:
            print(f"— {item.title}:")
            print(f"  {item.text_sample}")
            print()

    if len(measurements) > len(profiles):
        print()
        print(
            "среднее время: "
            f"{statistics.mean(m.seconds for m in measurements):.2f} с; "
            "средняя точность: "
            f"{statistics.mean(m.accuracy for m in measurements) * 100:.1f} %"
        )

    if arguments.json:
        arguments.json.write_text(
            json.dumps(
                [item.as_dict() for item in measurements],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nИзмерения записаны в {arguments.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
