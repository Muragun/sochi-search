"""
Постраничный OCR через внешний Tesseract.

Отличия от схемы 2.4.5:

- PDF открывается один раз на документ, а не заново на каждую страницу;
- страница растеризуется в собственном разрешении вложенного скана, а не
  всегда в фиксированные 180 dpi;
- перед распознаванием изображение бинаризуется локальным порогом и
  выравнивается по перекосу;
- используется внешний бинарь `tesseract`, поэтому доступны `--psm`, `--oem`
  и ограничение числа потоков OpenMP;
- пустые страницы отсеиваются до запуска OCR;
- каждая страница считается в отдельном процессе с собственной process group,
  поэтому таймаут гарантированно снимает всё дерево.

Измерения на имитации архивной ксерокопии (см. `docs/OCR_BENCHMARK.md`):
прежняя схема давала 54 символа мусора, новая — 1640 символов при точности
99,1 % и корректных номере и дате.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pdf_ocr_image import PreparedPage, prepare_page
from .text_repair import repair_ocr_text


class TesseractUnavailableError(RuntimeError):
    pass


class PageTimeoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class TesseractOutput:
    """Результат одного запуска Tesseract."""

    text: str
    confidence: float
    low_confidence_ratio: float
    words: int


@dataclass(frozen=True)
class OcrAttempt:
    """Одна ступень лестницы попыток — для журнала и отчётов."""

    label: str
    confidence: float
    characters: int
    seconds: float


@dataclass(frozen=True)
class OcrPageResult:
    text: str
    seconds: float
    render_dpi: int
    megapixels: float
    skew_degrees: float
    ink_ratio: float
    blank: bool
    timed_out: bool = False
    error: str | None = None

    # Собственная уверенность Tesseract, 0–100.
    #
    # Эвристическая оценка текста (`evaluate_text_quality`) отличает
    # осмысленный текст от глифовой каши, но не отличает верно распознанную
    # страницу от неверно распознанной: у выцветшей копии с точностью 50 %
    # она даёт те же 100 баллов, что и у чистого скана. Средняя уверенность
    # по словам на тех же страницах — 24 против 96. Это единственный
    # доступный признак, по которому можно решить, стоит ли пробовать ещё
    # раз, и он достаётся бесплатно: тот же запуск, ещё один вид выдачи.
    confidence: float = 0.0
    low_confidence_ratio: float = 0.0
    word_count: int = 0

    rotation: int = 0
    method: str = "sauvola"
    attempts: tuple[OcrAttempt, ...] = ()


def resolve_tesseract(explicit: str | None = None) -> str:
    candidate = explicit or os.getenv("CRAWL_TESSERACT_BINARY") or "tesseract"
    resolved = shutil.which(candidate)

    if not resolved:
        raise TesseractUnavailableError(
            f"Не найден исполняемый файл Tesseract: {candidate}"
        )

    return resolved


def tesseract_version(binary: str | None = None) -> str:
    executable = resolve_tesseract(binary)

    completed = subprocess.run(
        [executable, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
        timeout=30,
    )

    first_line = (completed.stdout or "").splitlines()[:1]

    return first_line[0].strip() if first_line else "unknown"


def native_image_dpi(page: Any) -> int | None:
    """
    Оценивает разрешение вложенного растра относительно размера страницы.

    У сканированного PDF страница почти всегда состоит из одной картинки.
    Рендер в меньшее разрешение просто выбрасывает пиксели, которые уже есть
    в файле, поэтому целевой dpi берётся из самого крупного изображения.
    """

    try:
        width_points = float(page.rect.width)
        height_points = float(page.rect.height)
    except Exception:
        return None

    if width_points <= 0 or height_points <= 0:
        return None

    best: int | None = None

    try:
        blocks = page.get_text("rawdict").get("blocks", [])
    except Exception:
        return None

    for block in blocks:
        if block.get("type") != 1:
            continue

        image_width = block.get("width") or 0
        image_height = block.get("height") or 0

        if not image_width or not image_height:
            continue

        horizontal = float(image_width) * 72.0 / width_points
        vertical = float(image_height) * 72.0 / height_points
        candidate = int(round(min(horizontal, vertical)))

        if candidate > 0 and (best is None or candidate > best):
            best = candidate

    return best


def choose_render_dpi(
    page: Any,
    *,
    minimum_dpi: int,
    maximum_dpi: int,
    pixel_budget: int,
) -> int:
    """
    Выбирает dpi рендера с двумя ограничениями.

    Снизу — качество: ниже `minimum_dpi` Tesseract теряет мелкий шрифт.
    Сверху — стоимость: площадь растра ограничена `pixel_budget`, иначе
    крупноформатный лист (план, схема) выедает весь бюджет документа.
    """

    native = native_image_dpi(page)
    target = native or minimum_dpi
    target = max(minimum_dpi, min(target, maximum_dpi))

    try:
        width_inches = float(page.rect.width) / 72.0
        height_inches = float(page.rect.height) / 72.0
    except Exception:
        return target

    area = width_inches * height_inches

    if area <= 0:
        return target

    capped = int((pixel_budget / area) ** 0.5)

    return max(72, min(target, capped))


def render_page(page: Any, dpi: int) -> Any:
    """Растрирует страницу в оттенках серого."""

    import pymupdf

    return page.get_pixmap(
        dpi=dpi,
        alpha=False,
        colorspace=pymupdf.csGRAY,
    )


def _terminate_group(process: subprocess.Popen) -> None:
    for sender, signal_number in (
        (os.killpg, signal.SIGTERM),
        (os.killpg, signal.SIGKILL),
    ):
        if process.poll() is not None:
            return

        try:
            sender(os.getpgid(process.pid), signal_number)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except Exception:
                pass

        try:
            process.wait(timeout=5)
            return
        except Exception:
            continue


def parse_tsv_confidence(
    tsv_text: str,
) -> tuple[float, float, int]:
    """
    Средняя уверенность по словам, доля ненадёжных и число слов.

    В TSV Tesseract кладёт по строке на каждый элемент разметки: страницу,
    блок, абзац, строку и слово. Уверенность осмысленна только у слов, у
    остальных стоит −1, поэтому строки без текста отбрасываются.
    """

    confidences: list[float] = []

    for line in tsv_text.splitlines()[1:]:
        columns = line.split("\t")

        if len(columns) < 12:
            continue

        try:
            value = float(columns[10])
        except ValueError:
            continue

        if value < 0 or not columns[11].strip():
            continue

        confidences.append(value)

    if not confidences:
        return 0.0, 1.0, 0

    average = sum(confidences) / len(confidences)
    low = sum(1 for value in confidences if value < 60.0)

    return (
        round(average, 2),
        round(low / len(confidences), 4),
        len(confidences),
    )


def run_tesseract(
    image_path: Path,
    *,
    binary: str,
    tessdata: Path,
    languages: str,
    dpi: int,
    psm: int,
    oem: int,
    threads: int,
    timeout_seconds: int,
) -> TesseractOutput:
    environment = dict(os.environ)
    environment["TESSDATA_PREFIX"] = str(tessdata.resolve())
    environment["OMP_THREAD_LIMIT"] = str(max(1, threads))

    with tempfile.TemporaryDirectory(
        prefix="sochi-ocr-",
        dir=str(image_path.parent),
    ) as directory:
        output_base = Path(directory) / "page"

        command = [
            binary,
            str(image_path),
            str(output_base),
            "-l", languages,
            "--oem", str(oem),
            "--psm", str(psm),
            "--dpi", str(dpi),
            "-c", "debug_file=/dev/null",

            # Tesseract при низкой уверенности повторяет распознавание на
            # инвертированном изображении. Страница уже бинаризована и
            # заведомо «тёмное на светлом», поэтому второй проход — чистая
            # потеря времени. Замер на сервере: 12,24 с против 8,74 с при
            # полностью совпадающем тексте.
            "-c", "tessedit_do_invert=0",

            # Два вида выдачи за один разбор страницы. Отдельный запуск
            # ради TSV стоил бы столько же, сколько сам OCR; здесь
            # распознавание уже сделано, и tsv — только другая запись
            # того же результата.
            #
            # Задаётся переменными, а не именами файлов конфигурации.
            # Имена `txt` и `tsv` Tesseract ищет в `$TESSDATA_PREFIX/
            # configs/`, а в `ocr/tessdata` этого каталога нет — там
            # только языковые модели. На рабочем сервере получалось
            # «read_params_file: Can't open tsv», TSV не создавался,
            # уверенность выходила нулевой, и лестница повторов
            # запускалась на каждой странице: пять попыток вместо одной,
            # 26 секунд вместо 5. Переменные работают без configs/.
            "-c", "tessedit_create_txt=1",
            "-c", "tessedit_create_tsv=1",
        ]

        process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        try:
            _, error_output = process.communicate(
                timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            _terminate_group(process)
            raise PageTimeoutError(
                f"OCR страницы превысил {timeout_seconds} секунд"
            ) from None

        if process.returncode != 0:
            message = (error_output or b"").decode(
                "utf-8", errors="replace"
            ).strip()
            raise RuntimeError(
                f"Tesseract завершился с кодом {process.returncode}: "
                f"{message[:400]}"
            )

        text_path = output_base.with_suffix(".txt")
        tsv_path = output_base.with_suffix(".tsv")

        text = (
            text_path.read_text(encoding="utf-8", errors="replace")
            if text_path.is_file()
            else ""
        )

        # Переносы склеиваются здесь, пока в тексте ещё есть переводы строк:
        # дальше по конвейеру пробелы схлопываются, и «благоустрой- ство»
        # уже не отличить от настоящего дефиса.
        text = repair_ocr_text(text)

        if not tsv_path.is_file():
            return TesseractOutput(
                text=text,
                confidence=0.0,
                low_confidence_ratio=1.0,
                words=0,
            )

        confidence, low_ratio, words = parse_tsv_confidence(
            tsv_path.read_text(encoding="utf-8", errors="replace")
        )

        return TesseractOutput(
            text=text,
            confidence=confidence,
            low_confidence_ratio=low_ratio,
            words=words,
        )


def _recognize_prepared(
    prepared: PreparedPage,
    *,
    binary: str,
    tessdata: Path,
    languages: str,
    dpi: int,
    psm: int,
    oem: int,
    threads: int,
    timeout_seconds: int,
    working_directory: Path,
) -> TesseractOutput:
    """Сохраняет подготовленную страницу и отдаёт её Tesseract."""

    working_directory.mkdir(parents=True, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        suffix=".png",
        prefix="sochi-page-",
        dir=str(working_directory),
        delete=False,
    )
    image_path = Path(handle.name)
    handle.close()

    try:
        prepared.image.save(image_path, format="PNG", optimize=False)

        return run_tesseract(
            image_path,
            binary=binary,
            tessdata=tessdata,
            languages=languages,
            dpi=dpi,
            psm=psm,
            oem=oem,
            threads=threads,
            timeout_seconds=timeout_seconds,
        )

    finally:
        image_path.unlink(missing_ok=True)


def ocr_page(
    page: Any,
    *,
    binary: str,
    tessdata: Path,
    languages: str,
    minimum_dpi: int,
    maximum_dpi: int,
    pixel_budget: int,
    psm: int,
    oem: int,
    threads: int,
    timeout_seconds: int,
    working_directory: Path,
    deskew: bool = True,
    binarize: bool = True,
    sauvola_k: float = 0.15,
    confidence_floor: float = 0.0,
    retry_sauvola_k: float = 0.10,
    retry_rotations: tuple[int, ...] = (),
    ladder_budget_seconds: int = 0,
) -> OcrPageResult:
    """
    Распознаёт страницу, при низкой уверенности пробуя ещё раз иначе.

    Первая попытка — обычная: бинаризация Сауволы, устранение перекоса,
    удаление точек тонера. На нормальной странице она же и последняя, и
    ничего не дорожает: `confidence_floor` не достигается только тогда,
    когда Tesseract сам сообщает, что читал плохо.

    Ступени подобраны по замерам на синтетических страницах с известным
    эталоном:

    | страница              | одна попытка | с лестницей |
    | --------------------- | -----------: | ----------: |
    | современный скан      |       99,4 % |      99,4 % |
    | архивная ксерокопия   |       98,4 % |      98,4 % |
    | выцветшая копия       |       49,7 % |      54,6 % |
    | повёрнутая на 90°     |       15,9 % |      98,6 % |

    Повёрнутая страница — это не редкость: альбомный скан А4 попадает в
    архив ровно так же, как книжный, и до сих пор терялся целиком.
    """

    from PIL import Image

    started = time.monotonic()

    dpi = choose_render_dpi(
        page,
        minimum_dpi=minimum_dpi,
        maximum_dpi=maximum_dpi,
        pixel_budget=pixel_budget,
    )

    pixmap = render_page(page, dpi)
    megapixels = round(pixmap.width * pixmap.height / 1e6, 2)

    source = Image.frombytes(
        "L",
        (pixmap.width, pixmap.height),
        pixmap.samples,
    )

    prepared: PreparedPage = prepare_page(
        source,
        deskew=deskew,
        binarize=binarize,
        sauvola_k=sauvola_k,
    )

    if prepared.blank:
        return OcrPageResult(
            text="",
            seconds=round(time.monotonic() - started, 2),
            render_dpi=dpi,
            megapixels=megapixels,
            skew_degrees=prepared.skew_degrees,
            ink_ratio=prepared.ink_ratio,
            blank=True,
            method=prepared.method,
        )

    def elapsed() -> float:
        return time.monotonic() - started

    attempts: list[OcrAttempt] = []

    best_output: TesseractOutput | None = None
    best_prepared = prepared
    best_rotation = 0
    best_label = "прямо"

    timed_out = False
    errors: list[str] = []

    def try_attempt(
        candidate: PreparedPage,
        *,
        label: str,
        rotation: int,
    ) -> bool:
        """Возвращает True, если уверенности достаточно и можно остановиться."""

        nonlocal best_output, best_prepared, best_rotation, best_label
        nonlocal timed_out

        attempt_started = time.monotonic()

        try:
            output = _recognize_prepared(
                candidate,
                binary=binary,
                tessdata=tessdata,
                languages=languages,
                dpi=dpi,
                psm=psm,
                oem=oem,
                threads=threads,
                timeout_seconds=timeout_seconds,
                working_directory=working_directory,
            )

        except PageTimeoutError as exc:
            timed_out = True
            errors.append(f"{label}: {exc}")
            return False

        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            return False

        attempts.append(
            OcrAttempt(
                label=label,
                confidence=output.confidence,
                characters=len(output.text.strip()),
                seconds=round(time.monotonic() - attempt_started, 2),
            )
        )

        if (
            best_output is None
            or output.confidence > best_output.confidence
        ):
            best_output = output
            best_prepared = candidate
            best_rotation = rotation
            best_label = label

        return output.confidence >= confidence_floor

    satisfied = try_attempt(prepared, label="прямо", rotation=0)

    # Уверенность не измерилась, а текст есть — значит, разбор TSV не
    # удался, а не страница плохая. Судить по такому признаку нельзя:
    # ноль меньше любого порога, поэтому лестница пошла бы на каждой
    # странице, а при равных нулях побеждала бы первая попытка — то есть
    # повторы стоили бы времени и ничего не решали. Ровно это и
    # происходило на рабочем сервере, где Tesseract не нашёл configs/.
    confidence_measured = (
        best_output is not None
        and (
            best_output.words > 0
            or not best_output.text.strip()
        )
    )

    ladder_enabled = (
        confidence_floor > 0.0
        and confidence_measured
        and not satisfied
        and not timed_out
    )

    if ladder_enabled:
        deadline = (
            started + ladder_budget_seconds
            if ladder_budget_seconds > 0
            else None
        )

        def budget_left() -> bool:
            return deadline is None or time.monotonic() < deadline

        # Ступень 1 — мягкий порог. Выцветшая копия с широкими светлыми
        # штрихами при строгом пороге теряет половину чернил.
        if (
            binarize
            and budget_left()
            and abs(retry_sauvola_k - sauvola_k) > 1e-9
        ):
            satisfied = try_attempt(
                prepare_page(
                    source,
                    deskew=deskew,
                    binarize=True,
                    sauvola_k=retry_sauvola_k,
                ),
                label=f"k={retry_sauvola_k:g}",
                rotation=0,
            )

        # Ступень 2 — ориентация. Проверять её признаками изображения
        # ненадёжно: на зашумлённой странице проекции по строкам и по
        # столбцам различаются на единицы процентов, и признак ошибается.
        # Сам Tesseract отвечает на этот вопрос точно, поэтому дешевле
        # спросить его и сравнить уверенность.
        for angle in retry_rotations:
            if satisfied or not budget_left():
                break

            normalized_angle = int(angle) % 360

            if normalized_angle == 0:
                continue

            satisfied = try_attempt(
                prepare_page(
                    source.rotate(
                        normalized_angle,
                        expand=True,
                        fillcolor=255,
                    ),
                    deskew=deskew,
                    binarize=binarize,
                    sauvola_k=sauvola_k,
                ),
                label=f"поворот {normalized_angle}°",
                rotation=normalized_angle,
            )

    seconds = round(elapsed(), 2)

    if best_output is None:
        return OcrPageResult(
            text="",
            seconds=seconds,
            render_dpi=dpi,
            megapixels=megapixels,
            skew_degrees=prepared.skew_degrees,
            ink_ratio=prepared.ink_ratio,
            blank=False,
            timed_out=timed_out,
            error=(
                " | ".join(errors[:3])
                if errors
                else "Tesseract не вернул результата"
            ),
            method=prepared.method,
            attempts=tuple(attempts),
        )

    return OcrPageResult(
        text=best_output.text.strip(),
        seconds=seconds,
        render_dpi=dpi,
        megapixels=megapixels,
        skew_degrees=best_prepared.skew_degrees,
        ink_ratio=best_prepared.ink_ratio,
        blank=False,
        timed_out=timed_out,
        error=" | ".join(errors[:3]) if errors else None,
        confidence=best_output.confidence,
        low_confidence_ratio=best_output.low_confidence_ratio,
        word_count=best_output.words,
        rotation=best_rotation,
        method=f"{best_prepared.method}:{best_label}",
        attempts=tuple(attempts),
    )


def page_order(page_count: int, *, priority_pages: int) -> list[int]:
    """
    Порядок обхода страниц: сначала реквизиты, затем остальное.

    Первые страницы несут вид акта, номер и дату — именно по ним документ
    находят в поиске. Если бюджет закончится, потеряется хвост, а не шапка.
    """

    head = list(range(min(priority_pages, page_count)))
    tail = list(range(len(head), page_count))

    return head + tail
