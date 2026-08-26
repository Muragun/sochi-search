#!/usr/bin/env python3
"""
Починка текста после распознавания.

Tesseract возвращает то, что увидел, а не то, что написано. Две поломки
встречаются на каждом архивном скане и обе бьют именно по поиску, а не по
чтению глазами.

**Перенос по слогам.** В вёрстке правовых актов строка выравнивается по
ширине, поэтому слова рвутся дефисом: «благоустрой-» на одной строке,
«ство» на следующей. После склейки строк в один абзац остаётся
«благоустрой- ство» — два токена, ни один из которых не найдётся по запросу
«благоустройство». На постановлении в тридцать строк таких разрывов
пять-семь, и все они приходятся на содержательные слова: короткие предлоги
не переносят.

**Латинские двойники кириллицы.** При `rus+eng` Tesseract свободно
смешивает алфавиты: «Сочи» приходит как «Coчи», номер «512-р» — как
«512-p» с латинской «p». Глазами не отличить, а для поиска это разные
строки. Настоящее слово никогда не смешивает две письменности, поэтому
смешанный токен чинится однозначно.

Модуль ничего не знает ни о PDF, ни о Elasticsearch: на вход строка, на
выход строка. Поэтому его же использует переиндексация, когда чинит текст,
уже лежащий в индексе.
"""

from __future__ import annotations

import re
import unicodedata

# Латинские буквы, начертание которых совпадает с кириллическими.
#
# Только полные двойники: «m» похожа на «т» лишь в рукописном начертании,
# и включать её нельзя — в печатном тексте это разные знаки.
LATIN_TO_CYRILLIC = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у",
}

CYRILLIC_TO_LATIN = {
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K",
    "М": "M", "О": "O", "Р": "P", "Т": "T", "Х": "X", "У": "Y",
    "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x",
    "у": "y",
}

LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

# Перенос: буквы, дефис, конец строки, буквы. Учитываются и мягкий перенос,
# и обычный дефис, и его типографские двойники.
HYPHEN_BREAK_RE = re.compile(
    r"([А-Яа-яЁёA-Za-z]{2,})"
    r"[-­‐‑‒–—]"
    r"[ \t]*\r?\n[ \t]*"
    r"([а-яёa-z]{2,})"
)

# Номер акта: цифры, дефис, короткий буквенный индекс.
DOCUMENT_NUMBER_SUFFIX_RE = re.compile(
    r"(\d{1,7})\s*[-‐‑‒–—−]\s*([A-Za-zА-Яа-яЁё]{1,3})\b"
)

TOKEN_RE = re.compile(r"[^\W\d_]+", flags=re.UNICODE)

SOFT_HYPHEN = "­"

# Пробелы, которые выглядят как пробел, но им не являются. Внутри «№ 512»
# неразрывный пробел превращает номер в один нерасщепляемый токен.
EXOTIC_SPACES = (
    " ",  # неразрывный
    " ",  # цифровой
    " ",  # тонкий
    " ",  # узкий неразрывный
    "﻿",  # нулевой ширины
)


def join_hyphenated_line_breaks(value: str) -> str:
    """
    Склеивает слово, разорванное переносом на границе строк.

    Работает до нормализации пробелов: после неё перевод строки уже потерян,
    а отличить перенос от настоящего дефиса в «город-курорт» больше не по
    чему. Поэтому вызывать нужно на сыром выводе OCR.
    """

    text = str(value or "")

    if not text:
        return ""

    previous = None

    # Два переноса подряд встречаются в узких колонках таблиц.
    while previous != text:
        previous = text
        text = HYPHEN_BREAK_RE.sub(r"\1\2", text)

    return text


def _is_mixed_script(token: str) -> bool:
    return bool(
        LATIN_RE.search(token)
        and CYRILLIC_RE.search(token)
    )


def _to_cyrillic(token: str) -> str | None:
    """Переводит латинские двойники в кириллицу или отказывается."""

    result: list[str] = []

    for character in token:
        if LATIN_RE.match(character):
            replacement = LATIN_TO_CYRILLIC.get(character)

            if replacement is None:
                return None

            result.append(replacement)
        else:
            result.append(character)

    return "".join(result)


def _to_latin(token: str) -> str | None:
    result: list[str] = []

    for character in token:
        if CYRILLIC_RE.match(character):
            replacement = CYRILLIC_TO_LATIN.get(character)

            if replacement is None:
                return None

            result.append(replacement)
        else:
            result.append(character)

    return "".join(result)


def repair_mixed_script_word(token: str) -> str:
    """
    Приводит смешанный токен к одной письменности.

    Решает большинство: если кириллических букв больше, лишние латинские
    считаются ошибкой распознавания, и наоборот. Если перевести удаётся
    только в одну сторону — выбирается она. Если ни в одну, токен остаётся
    как есть: лучше не тронуть, чем испортить.
    """

    if not _is_mixed_script(token):
        return token

    cyrillic_count = len(CYRILLIC_RE.findall(token))
    latin_count = len(LATIN_RE.findall(token))

    as_cyrillic = _to_cyrillic(token)
    as_latin = _to_latin(token)

    if cyrillic_count >= latin_count:
        return as_cyrillic or as_latin or token

    return as_latin or as_cyrillic or token


def repair_mixed_script(value: str) -> str:
    """Чинит смешанные токены во всём тексте."""

    text = str(value or "")

    if not text:
        return ""

    def replace(match: "re.Match[str]") -> str:
        return repair_mixed_script_word(match.group(0))

    return TOKEN_RE.sub(replace, text)


def repair_document_number_suffix(value: str) -> str:
    """
    Возвращает кириллицу буквенному индексу номера акта.

    «512-p» с латинской «p» и «512-р» с кириллической выглядят одинаково, а
    ищутся по-разному. Номер акта в этом корпусе всегда русский, поэтому
    однобуквенный латинский хвост при цифрах — это ошибка распознавания.
    Правило намеренно узкое: только цифры, дефис и не больше трёх букв.
    """

    text = str(value or "")

    if not text:
        return ""

    def replace(match: "re.Match[str]") -> str:
        digits, suffix = match.group(1), match.group(2)

        if not LATIN_RE.search(suffix):
            return match.group(0)

        converted = _to_cyrillic(suffix)

        if converted is None:
            return match.group(0)

        return f"{digits}-{converted}"

    return DOCUMENT_NUMBER_SUFFIX_RE.sub(replace, text)


def repair_ocr_text(
    value: str,
    *,
    join_hyphens: bool = True,
    fix_mixed_script: bool = True,
    fix_document_numbers: bool = True,
) -> str:
    """
    Полная починка вывода OCR.

    Порядок важен: переносы склеиваются первыми, пока в строке ещё есть
    переводы строк, и только потом чинится письменность — иначе разорванное
    слово чинилось бы половинками.
    """

    text = str(value or "")

    if not text:
        return ""

    # Мягкий перенос на конце строки — такой же перенос, как обычный дефис,
    # и склеивается тем же правилом: он входит в набор знаков переноса.
    # Убирать его заранее нельзя, иначе склеивать станет нечего.
    if join_hyphens:
        text = join_hyphenated_line_breaks(text)

    # Оставшиеся мягкие переносы невидимы, но делят слово на два токена.
    text = text.replace(SOFT_HYPHEN, "")

    if fix_mixed_script:
        text = repair_mixed_script(text)

    if fix_document_numbers:
        text = repair_document_number_suffix(text)

    for exotic_space in EXOTIC_SPACES:
        text = text.replace(exotic_space, " ")

    # NFC, а не NFKC.
    #
    # Нужное здесь — сборка разложенных букв: Tesseract иногда отдаёт «й»
    # как «и» с отдельным знаком краткости, и в индексе это два разных
    # слова. NFC их склеивает.
    #
    # NFKC сделал бы это тоже, но заодно переписал бы «№» в «No», «…» в
    # три точки, «Ⅳ» в «IV». Знак номера стоит в шапке каждого акта и
    # показывается пользователю в карточке результата: молча подменять его
    # нельзя. Совместимые формы вроде лигатур в русских сканах не
    # встречаются, так что терять нечего.
    return unicodedata.normalize("NFC", text)


# Имя файла, в котором нет смысла: длинная сплошная строка из букв и
# цифр без разделителей. Битрикс раскладывает вложения по
# `/upload/iblock/` под такими именами — и hex, и base36:
#
#     028e97063df6b807dc50e5901603af2e.docx
#     tg7dcgl1954sjipys673nrc12y1zd6o0.docx
#
# Показывать такое как название документа нельзя: в выдаче получается
# столбец неразличимых строк, по которому невозможно выбрать нужное.
#
# Настоящее имя от машинного отличается разделителями, кириллицей или
# длиной: «postanovlenie-512.pdf», «отчёт за 2024.xlsx». Цифра в строке
# обязательна — сплошное слово из 24 букв само по себе неправдоподобно,
# но требование делает правило заведомо безопасным.
TECHNICAL_FILENAME_RE = re.compile(
    r"^(?:"
    r"(?=[a-z0-9]*\d)[a-z0-9]{24,64}"
    r"|[0-9a-f]{8}-[0-9a-f-]{20,28}"
    r")(?:\.[a-z0-9]{1,5})?$",
    flags=re.IGNORECASE,
)


def technical_filename(value: str) -> bool:
    """Имя файла машинное, а не осмысленное."""

    return bool(
        TECHNICAL_FILENAME_RE.fullmatch(
            str(value or "").strip()
        )
    )


DASHES = "‐‑‒–—―−"

NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:№|N[оo]?\.?)\s*",
    flags=re.IGNORECASE,
)

# Латинские двойники в номерах актов приводятся к кириллице целиком, без
# оглядки на остальные буквы: номер в этом корпусе всегда русский.
NUMBER_HOMOGLYPHS = str.maketrans(
    {
        "p": "р", "P": "р", "c": "с", "C": "с",
        "a": "а", "A": "а", "o": "о", "O": "о",
        "e": "е", "E": "е", "x": "х", "X": "х",
        "y": "у", "Y": "у", "k": "к", "K": "к",
        "m": "м", "M": "м", "t": "т", "T": "т",
        "h": "н", "H": "н", "b": "в", "B": "в",
    }
)


def normalize_document_number(value: str) -> str:
    """
    Приводит «№ 512 – Р», «N512-P» и «512р» к одному виду.

    Единственная реализация на весь проект. Раньше их было две: одна
    строила запрос, вторая — нормализатор Elasticsearch в схеме индекса, и
    они расходились. Из семи способов записать один и тот же номер точное
    совпадение срабатывало на двух: «№ 512-р» лежал в индексе как
    «№  512-р», а искался как «512-р».

    Поэтому и запрос, и поле `document_number_normalized` считаются здесь.
    Совпадение перестаёт зависеть от того, как номер записан в источнике.
    """

    normalized = str(value or "").strip()
    normalized = normalized.replace(" ", " ")

    for dash in DASHES:
        normalized = normalized.replace(dash, "-")

    normalized = NUMBER_PREFIX_RE.sub("", normalized)
    normalized = normalized.translate(NUMBER_HOMOGLYPHS)

    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized)

    return normalized.strip().casefold()


def count_mixed_script_tokens(value: str) -> int:
    """Сколько токенов смешивают письменности. Нужно для отчётов и тестов."""

    return sum(
        1
        for token in TOKEN_RE.findall(str(value or ""))
        if _is_mixed_script(token)
    )
