from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Iterable


# v6 — внешний Tesseract с бинаризацией Сауволы, устранением перекоса и
# рендером в родном разрешении скана. Строку обязательно поднимать при
# любом изменении, влияющем на результат распознавания: именно по ней
# фоновый проход отбирает документы, которые нужно переделать.
PDF_EXTRACTION_VERSION = "pdf-ocr-v6"
PDF_FAST_EXTRACTION_VERSION = "pdf-native-v1"

WHITESPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(
    r"[A-Za-zА-Яа-яЁё]{2,}",
)

RUSSIAN_COMMON_WORDS = {
    "администрация",
    "города",
    "город",
    "сочи",
    "постановление",
    "распоряжение",
    "решение",
    "приказ",
    "указ",
    "муниципального",
    "образования",
    "краснодарского",
    "края",
    "внести",
    "утверждении",
    "настоящее",
    "приложение",
    "соответствии",
    "года",
}

ENGLISH_COMMON_WORDS = {
    "a",
    "an",
    "and",
    "administration",
    "are",
    "as",
    "at",
    "by",
    "city",
    "document",
    "for",
    "from",
    "in",
    "is",
    "of",
    "official",
    "on",
    "or",
    "ready",
    "resolution",
    "the",
    "this",
    "to",
    "with",
}

CYRILLIC_VOWELS = set("аеёиоуыэюя")
LATIN_VOWELS = set("aeiouy")
SUSPICIOUS_SYMBOLS = set("{}[]\\|^~`_")

LEGAL_HEADER_KIND_RE = re.compile(
    (
        r"\b(?:постановление|распоряжение|решение|"
        r"приказ|положение|указ)\b"
    ),
    flags=re.IGNORECASE,
)

LEGAL_HEADER_NUMBER_RE = re.compile(
    (
        r"(?:№|N[oо]?\.?)\s*"
        r"\d{1,7}"
        r"(?:\s*[-–—/]\s*[A-Za-zА-Яа-яЁё0-9]+)?"
    ),
    flags=re.IGNORECASE,
)

LEGAL_HEADER_DATE_RE = re.compile(
    (
        r"\bот\s+"
        r"(?:"
        r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}"
        r"|"
        r"\d{1,2}\s+"
        r"(?:января|февраля|марта|апреля|мая|июня|июля|"
        r"августа|сентября|октября|ноября|декабря)\s+"
        r"\d{4}(?:\s+года)?"
        r")"
    ),
    flags=re.IGNORECASE,
)

RUSSIAN_MONTH_NUMBERS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


@dataclass(frozen=True)
class TextQuality:
    score: int
    readable: bool
    reason: str
    characters: int
    letters: int
    words: int
    cyrillic_ratio: float
    latin_ratio: float
    suspicious_ratio: float


@dataclass(frozen=True)
class PdfPageText:
    text: str
    method: str
    quality: TextQuality
    ocr_attempted: bool
    rotation: int | None = None
    error: str | None = None


def compact_text(value: str) -> str:
    return WHITESPACE_RE.sub(
        " ",
        value or "",
    ).strip()


def _is_cyrillic(character: str) -> bool:
    return (
        "а" <= character.casefold() <= "я"
        or character.casefold() == "ё"
    )


def _is_latin(character: str) -> bool:
    lowered = character.casefold()
    return "a" <= lowered <= "z"


def _word_has_vowel(word: str) -> bool:
    lowered = word.casefold()
    return any(
        character in CYRILLIC_VOWELS
        or character in LATIN_VOWELS
        for character in lowered
    )


def _mixed_case_word(word: str) -> bool:
    if len(word) < 4:
        return False

    return not (
        word.islower()
        or word.isupper()
        or word.istitle()
    )


def evaluate_text_quality(
    value: str,
    *,
    minimum_length: int = 80,
) -> TextQuality:
    """
    Оценивает не только объём, но и правдоподобие текстового слоя.

    Старые PDF sochi.ru часто содержат длинную последовательность латинских
    глифов вместо русского текста. Проверка длины считает её пригодной, поэтому
    отдельно учитываются письменность, обычные слова, регистр и мусорные знаки.
    """

    text = compact_text(value)
    characters = len(text)
    letters_list = [
        character
        for character in text
        if character.isalpha()
    ]
    letters = len(letters_list)
    digits = sum(
        character.isdigit()
        for character in text
    )
    words = WORD_RE.findall(text)
    word_count = len(words)

    cyrillic = sum(
        _is_cyrillic(character)
        for character in letters_list
    )
    latin = sum(
        _is_latin(character)
        for character in letters_list
    )

    cyrillic_ratio = (
        cyrillic / letters
        if letters
        else 0.0
    )
    latin_ratio = (
        latin / letters
        if letters
        else 0.0
    )

    suspicious_characters = sum(
        character in SUSPICIOUS_SYMBOLS
        for character in text
    )
    suspicious_ratio = (
        suspicious_characters / characters
        if characters
        else 0.0
    )

    lowered_words = [
        word.casefold()
        for word in words
    ]
    russian_hits = sum(
        word in RUSSIAN_COMMON_WORDS
        for word in lowered_words
    )
    english_hits = sum(
        word in ENGLISH_COMMON_WORDS
        for word in lowered_words
    )
    vowel_words = sum(
        _word_has_vowel(word)
        for word in words
    )
    vowel_ratio = (
        vowel_words / word_count
        if word_count
        else 0.0
    )
    mixed_case_words = sum(
        _mixed_case_word(word)
        for word in words
    )
    mixed_case_ratio = (
        mixed_case_words / word_count
        if word_count
        else 0.0
    )
    average_word_length = (
        sum(len(word) for word in words)
        / word_count
        if word_count
        else 0.0
    )
    short_word_ratio = (
        sum(
            len(word) <= 2
            for word in words
        )
        / word_count
        if word_count
        else 0.0
    )

    printable_ratio = (
        sum(
            character.isprintable()
            for character in text
        )
        / characters
        if characters
        else 0.0
    )
    alphanumeric_ratio = (
        (letters + digits) / characters
        if characters
        else 0.0
    )

    russian_like = (
        cyrillic_ratio >= 0.28
        and word_count >= 3
        and average_word_length >= 3.1
    )
    required_english_hits = max(
        2,
        math.ceil(word_count * 0.04),
    )
    english_like = (
        latin_ratio >= 0.70
        and english_hits >= required_english_hits
    )
    numeric_table_like = (
        digits >= 20
        and alphanumeric_ratio >= 0.55
        and word_count >= 3
    )

    score = 0

    if characters >= minimum_length:
        score += 15
    elif characters >= max(20, minimum_length // 2):
        score += 7

    if printable_ratio >= 0.98:
        score += 10
    elif printable_ratio >= 0.90:
        score += 5

    if alphanumeric_ratio >= 0.55:
        score += 10
    elif alphanumeric_ratio >= 0.35:
        score += 5

    if word_count >= 8:
        score += 10
    elif word_count >= 3:
        score += 5

    if russian_like:
        score += 38
    elif english_like:
        score += 34
    elif numeric_table_like:
        score += 20

    if vowel_ratio >= 0.65:
        score += 8
    elif vowel_ratio >= 0.40:
        score += 4

    score += min(
        9,
        (russian_hits + english_hits) * 3,
    )

    unknown_latin_layer = (
        letters >= 40
        and latin_ratio >= 0.70
        and not english_like
    )
    fragmented_layer = (
        word_count >= 10
        and average_word_length < 3.1
        and short_word_ratio >= 0.45
    )
    implausible_mixed_script = (
        letters >= 40
        and 0.18 <= cyrillic_ratio <= 0.75
        and latin_ratio >= 0.20
        and (russian_hits + english_hits) < 2
        and short_word_ratio >= 0.35
    )

    if unknown_latin_layer:
        score -= 35

    if fragmented_layer:
        score -= 30

    if implausible_mixed_script:
        score -= 25

    if mixed_case_ratio >= 0.25:
        score -= 22
    elif mixed_case_ratio >= 0.12:
        score -= 10

    if suspicious_ratio >= 0.025:
        score -= 20
    elif suspicious_ratio >= 0.01:
        score -= 8

    score = max(0, min(100, score))

    if characters < minimum_length:
        readable = False
        reason = "too_short"
    elif letters < 20 and not numeric_table_like:
        readable = False
        reason = "too_few_letters"
    elif unknown_latin_layer:
        readable = False
        reason = "unknown_latin_glyph_layer"
    elif fragmented_layer:
        readable = False
        reason = "fragmented_words"
    elif implausible_mixed_script:
        readable = False
        reason = "implausible_mixed_script"
    elif mixed_case_ratio >= 0.25:
        readable = False
        reason = "suspicious_mixed_case"
    elif suspicious_ratio >= 0.025:
        readable = False
        reason = "suspicious_symbols"
    elif score < 55:
        readable = False
        reason = "low_quality_score"
    else:
        readable = True
        reason = "readable"

    return TextQuality(
        score=score,
        readable=readable,
        reason=reason,
        characters=characters,
        letters=letters,
        words=word_count,
        cyrillic_ratio=round(
            cyrillic_ratio,
            4,
        ),
        latin_ratio=round(
            latin_ratio,
            4,
        ),
        suspicious_ratio=round(
            suspicious_ratio,
            4,
        ),
    )


def looks_like_garbled_text(
    value: str,
    *,
    minimum_length: int = 80,
) -> bool:
    text = compact_text(value)

    if len(text) < minimum_length:
        return False

    return not evaluate_text_quality(
        text,
        minimum_length=minimum_length,
    ).readable


def _header_date_is_valid(value: str) -> bool:
    normalized = compact_text(value).casefold()
    normalized = re.sub(
        r"^от\s+",
        "",
        normalized,
    )
    normalized = re.sub(
        r"\s+года$",
        "",
        normalized,
    )

    numeric_match = re.fullmatch(
        r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})",
        normalized,
    )

    if numeric_match:
        day, month, year = map(
            int,
            numeric_match.groups(),
        )
    else:
        word_match = re.fullmatch(
            r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})",
            normalized,
        )

        if not word_match:
            return False

        day = int(word_match.group(1))
        month = RUSSIAN_MONTH_NUMBERS.get(
            word_match.group(2),
            0,
        )
        year = int(word_match.group(3))

    try:
        date(year, month, day)
    except ValueError:
        return False

    return True


def legal_header_is_reliable(value: str) -> bool:
    text = compact_text(value)
    kind_match = LEGAL_HEADER_KIND_RE.search(
        text[:1200]
    )

    if kind_match is None:
        return False

    header = text[
        kind_match.start():
        kind_match.start() + 320
    ]
    number_match = (
        LEGAL_HEADER_NUMBER_RE.search(
            header
        )
    )
    date_match = (
        LEGAL_HEADER_DATE_RE.search(
            header
        )
    )

    return (
        number_match is not None
        and date_match is not None
        and _header_date_is_valid(
            date_match.group(0)
        )
    )


def legal_header_needs_ocr(value: str) -> bool:
    """
    Определяет частично повреждённую первую страницу правового акта.

    У старых PDF sochi.ru основной русский текст иногда читается нормально,
    но номер и дата в шапке представлены неверными глифами. Общая оценка
    качества тогда не запускает OCR и универсальный парсер может принять дату
    события из заголовка за дату документа. Проверяем только короткую область
    рядом с первым видом правового акта в верхней части страницы.
    """

    text = compact_text(value)

    return (
        LEGAL_HEADER_KIND_RE.search(
            text[:1200]
        )
        is not None
        and not legal_header_is_reliable(
            text
        )
    )


def validate_tessdata(
    directory: Path,
    languages: str,
) -> tuple[Path, ...]:
    normalized_directory = directory.resolve()
    requested = [
        language.strip()
        for language in languages.split("+")
        if language.strip()
    ]

    if not requested:
        raise RuntimeError(
            "Не заданы языки PDF OCR"
        )

    files = tuple(
        normalized_directory
        / f"{language}.traineddata"
        for language in requested
    )
    missing = [
        str(path)
        for path in files
        if not path.is_file()
        or path.stat().st_size < 100_000
    ]

    if missing:
        raise RuntimeError(
            "Отсутствуют данные PDF OCR: "
            + ", ".join(missing)
        )

    return files


def _normalized_rotations(
    values: Iterable[int],
) -> tuple[int, ...]:
    result: list[int] = []

    for raw_value in values:
        value = int(raw_value) % 360

        if value not in {0, 90, 180, 270}:
            continue

        if value not in result:
            result.append(value)

    if 0 not in result:
        result.insert(0, 0)

    return tuple(result)


def _acceptable_ocr_quality(
    quality: TextQuality,
) -> bool:
    if quality.readable:
        return True

    # После физического поворота координаты отдельных OCR-блоков иногда
    # склеиваются (например, "мартаПОСТАНОВЛЕНИЕ"). Если сама строка почти
    # полностью кириллическая и набрала высокий балл, это всё равно намного
    # полезнее исходного битого глифового слоя.
    return (
        quality.score >= 70
        and quality.cyrillic_ratio >= 0.65
        and quality.words >= 8
    )


def extract_pdf_page_text(
    page: Any,
    native_text: str,
    *,
    tessdata: Path,
    languages: str,
    dpi: int,
    rotations: Iterable[int],
    minimum_length: int,
    ocr_enabled: bool = True,
    force_ocr: bool = False,
) -> PdfPageText:
    native = compact_text(native_text)
    native_quality = evaluate_text_quality(
        native,
        minimum_length=minimum_length,
    )

    if (
        native_quality.readable
        and not force_ocr
    ):
        return PdfPageText(
            text=native,
            method="native",
            quality=native_quality,
            ocr_attempted=False,
        )

    if not ocr_enabled:
        if native_quality.readable:
            return PdfPageText(
                text=native,
                method="native",
                quality=native_quality,
                ocr_attempted=False,
                error=(
                    "OCR шапки отключён"
                ),
            )

        return PdfPageText(
            text="",
            method="unreadable",
            quality=native_quality,
            ocr_attempted=False,
            error=(
                "OCR отключён; "
                f"native={native_quality.reason}"
            ),
        )

    validate_tessdata(
        tessdata,
        languages,
    )

    original_rotation = int(
        getattr(page, "rotation", 0)
        or 0
    ) % 360
    best_text = ""
    best_quality = evaluate_text_quality(
        "",
        minimum_length=minimum_length,
    )
    best_rotation: int | None = None
    errors: list[str] = []

    def acceptable_candidate(
        text: str,
        quality: TextQuality,
    ) -> bool:
        return (
            _acceptable_ocr_quality(
                quality
            )
            and (
                not force_ocr
                or legal_header_is_reliable(
                    text
                )
            )
        )

    try:
        for relative_rotation in (
            _normalized_rotations(rotations)
        ):
            target_rotation = (
                original_rotation
                + relative_rotation
            ) % 360

            try:
                page.set_rotation(
                    target_rotation
                )

                text_page = (
                    page.get_textpage_ocr(
                        language=languages,
                        dpi=dpi,
                        full=True,
                        tessdata=str(
                            tessdata.resolve()
                        ),
                    )
                )
                candidate = compact_text(
                    page.get_text(
                        "text",
                        textpage=text_page,
                        sort=True,
                    )
                )
                quality = evaluate_text_quality(
                    candidate,
                    minimum_length=minimum_length,
                )

            except Exception as exc:
                errors.append(
                    f"rotation={relative_rotation}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            acceptable = acceptable_candidate(
                candidate,
                quality,
            )
            candidate_rank = (
                int(acceptable),
                quality.score,
                len(candidate),
            )
            best_rank = (
                int(
                    acceptable_candidate(
                        best_text,
                        best_quality,
                    )
                ),
                best_quality.score,
                len(best_text),
            )

            if candidate_rank > best_rank:
                best_text = candidate
                best_quality = quality
                best_rotation = (
                    relative_rotation
                )

            if (
                acceptable
                and quality.score >= 78
            ):
                break

    finally:
        try:
            page.set_rotation(
                original_rotation
            )
        except Exception:
            pass

    if (
        acceptable_candidate(
            best_text,
            best_quality,
        )
        and best_text
    ):
        if not best_quality.readable:
            best_quality = replace(
                best_quality,
                readable=True,
                reason=(
                    "ocr_recovered_cyrillic"
                ),
            )

        return PdfPageText(
            text=best_text,
            method="ocr",
            quality=best_quality,
            ocr_attempted=True,
            rotation=best_rotation,
            error=(
                " | ".join(errors[:3])
                if errors
                else None
            ),
        )

    if native_quality.readable:
        return PdfPageText(
            text=native,
            method="native",
            quality=native_quality,
            ocr_attempted=True,
            error=(
                (
                    "OCR шапки не восстановил надёжные "
                    "номер и дату"
                    if force_ocr
                    else "OCR не дал читаемого результата"
                )
                + (
                    ": " + " | ".join(errors[:3])
                    if errors
                    else ""
                )
            ),
        )

    details = [
        (
            "native="
            f"{native_quality.reason}:"
            f"{native_quality.score}"
        )
    ]

    if best_text:
        details.append(
            "ocr="
            f"{best_quality.reason}:"
            f"{best_quality.score}"
        )

    details.extend(errors[:3])

    return PdfPageText(
        text="",
        method="unreadable",
        quality=best_quality,
        ocr_attempted=True,
        rotation=best_rotation,
        error=" | ".join(details),
    )


def run_ocr_self_test(
    tessdata: Path,
    *,
    languages: str = "rus+eng",
    dpi: int = 220,
) -> str:
    """Проверяет встроенный в PyMuPDF Tesseract без внешнего CLI."""

    # Импорт локальный: модуль целиком подключается поиском ради
    # `looks_like_garbled_text`, и тянуть туда PDF-библиотеку не нужно.
    # Раньше сбой pymupdf ронял API поиска на старте.
    import pymupdf

    validate_tessdata(
        tessdata,
        languages,
    )

    source_document = pymupdf.open()
    scan_document = pymupdf.open()
    opened_scan: Any = None

    try:
        source_page = source_document.new_page(
            width=900,
            height=480,
        )
        source_page.insert_htmlbox(
            pymupdf.Rect(
                40,
                40,
                860,
                440,
            ),
            (
                '<div style="font-size: 28pt; line-height: 1.4">'
                "THE OFFICIAL CITY DOCUMENT<br>"
                "АДМИНИСТРАЦИЯ ГОРОДА СОЧИ<br>"
                "ПОСТАНОВЛЕНИЕ № 223"
                "</div>"
            ),
        )
        pixmap = source_page.get_pixmap(
            dpi=180,
            alpha=False,
        )

        scan_page = scan_document.new_page(
            width=900,
            height=480,
        )
        scan_page.insert_image(
            scan_page.rect,
            pixmap=pixmap,
        )

        opened_scan = pymupdf.open(
            stream=scan_document.tobytes(),
            filetype="pdf",
        )
        page = opened_scan.load_page(0)
        native = page.get_text(
            "text",
            sort=True,
        )
        result = extract_pdf_page_text(
            page,
            native,
            tessdata=tessdata,
            languages=languages,
            dpi=dpi,
            rotations=(0, 180, 90, 270),
            minimum_length=40,
        )

        normalized = result.text.casefold()
        languages_set = {
            language.strip().casefold()
            for language in languages.split("+")
            if language.strip()
        }
        russian_ok = (
            "rus" not in languages_set
            or (
                "сочи" in normalized
                and "постановление" in normalized
            )
        )

        if (
            result.method != "ocr"
            or "official" not in normalized
            or "document" not in normalized
            or not russian_ok
        ):
            raise RuntimeError(
                "PDF OCR self-test не распознал "
                f"контрольную строку: {result}"
            )

        return result.text

    finally:
        if opened_scan is not None:
            opened_scan.close()

        scan_document.close()
        source_document.close()
