"""
Лестница повторов и уверенность Tesseract.

Здесь не запускается ни Tesseract, ни PyMuPDF: проверяется решение, а не
распознавание. Вопрос, на который отвечают тесты, один — при каких условиях
конвейер тратит время на вторую попытку. Ошибиться тут дорого в обе
стороны: лишний повтор умножает время на всём корпусе, пропущенный —
оставляет страницу нечитаемой навсегда.

Качество самого распознавания измеряется отдельно, в `ops/ocr_benchmark.py`,
на страницах с известным эталоном.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Пропуск объявляется до импорта numpy и Pillow, а не после.
#
# Модуль строит страницы массивами, поэтому без этих библиотек его тесты
# бессмысленны — но `import numpy` на голой машине бросал ImportError, и
# `unittest discover` показывал ошибку загрузки вместо пропуска. Роль
# `tests` в образе обещает обратное: «тесты, которым нужны отсутствующие
# библиотеки, объявляют пропуск сами». Здесь это обещание не работало.
try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover — среда без numpy или Pillow
    raise unittest.SkipTest(
        "нужны numpy и Pillow: тесты собирают страницы массивами"
    )

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler_v2 import pdf_ocr_engine  # noqa: E402
from crawler_v2.pdf_ocr_engine import (  # noqa: E402
    TesseractOutput,
    ocr_page,
    parse_tsv_confidence,
)


TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
    "left\ttop\twidth\theight\tconf\ttext"
)


def tsv_document(rows: list[tuple[float, str]]) -> str:
    lines = [TSV_HEADER]

    for confidence, word in rows:
        lines.append(
            "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t"
            f"{confidence}\t{word}"
        )

    return "\n".join(lines)


class ConfidenceParsingTests(unittest.TestCase):
    def test_averages_word_confidence(self) -> None:
        confidence, low_ratio, words = parse_tsv_confidence(
            tsv_document([(90.0, "город"), (80.0, "Сочи")])
        )

        self.assertEqual(confidence, 85.0)
        self.assertEqual(low_ratio, 0.0)
        self.assertEqual(words, 2)

    def test_ignores_structural_rows_without_text(self) -> None:
        """У страницы, блока и строки уверенность −1: она не про текст."""

        document = "\n".join(
            [
                TSV_HEADER,
                "1\t1\t0\t0\t0\t0\t0\t0\t10\t10\t-1\t",
                "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t95\tСочи",
            ]
        )

        confidence, _, words = parse_tsv_confidence(document)

        self.assertEqual(confidence, 95.0)
        self.assertEqual(words, 1)

    def test_counts_share_of_unreliable_words(self) -> None:
        _, low_ratio, _ = parse_tsv_confidence(
            tsv_document(
                [(90.0, "город"), (20.0, "Cоsи"), (10.0, "3aл")]
            )
        )

        self.assertAlmostEqual(low_ratio, 2 / 3, places=3)

    def test_empty_page_is_fully_unreliable(self) -> None:
        confidence, low_ratio, words = parse_tsv_confidence(TSV_HEADER)

        self.assertEqual(confidence, 0.0)
        self.assertEqual(low_ratio, 1.0)
        self.assertEqual(words, 0)

    def test_malformed_lines_are_skipped(self) -> None:
        document = "\n".join(
            [TSV_HEADER, "мусор", "5\t1\t1", ""]
        )

        confidence, _, words = parse_tsv_confidence(document)

        self.assertEqual(confidence, 0.0)
        self.assertEqual(words, 0)


class TesseractCommandTests(unittest.TestCase):
    """
    Вызов Tesseract не должен зависеть от содержимого configs/.

    Виды выдачи задавались именами файлов конфигурации — `txt` и `tsv`.
    Tesseract ищет их в `$TESSDATA_PREFIX/configs/`, а в `ocr/tessdata`
    лежат только языковые модели, без этого каталога. На рабочем сервере
    выходило «read_params_file: Can't open tsv», TSV не создавался,
    уверенность получалась нулевой — и лестница повторов запускалась на
    каждой странице: пять попыток вместо одной, 26 секунд вместо пяти.
    Повёрнутая страница при этом не восстанавливалась, потому что при
    равных нулях побеждала первая попытка.

    Замеры делались с системной tessdata, где configs/ есть, поэтому
    стенд расхождения не показывал.
    """

    def setUp(self) -> None:
        self.source = (
            Path(__file__).resolve().parents[1]
            / "crawler_v2"
            / "pdf_ocr_engine.py"
        ).read_text(encoding="utf-8")

    def test_output_kinds_are_set_by_variables(self) -> None:
        self.assertIn('"tessedit_create_tsv=1"', self.source)
        self.assertIn('"tessedit_create_txt=1"', self.source)

    def test_no_bare_config_file_names_in_command(self) -> None:
        command = self.source[
            self.source.index("command = ["):
            self.source.index("process = subprocess.Popen")
        ]

        for name in ('"txt",', '"tsv",'):
            self.assertNotIn(
                name,
                command,
                msg=(
                    f"{name} передаётся как имя файла конфигурации. "
                    "Tesseract ищет его в configs/, которого в "
                    "ocr/tessdata нет"
                ),
            )

    def test_repo_tessdata_has_no_configs_directory(self) -> None:
        """
        Фиксируем причину, а не только следствие.

        Если каталог однажды появится, тест напомнит, что вызов больше не
        обязан обходиться без него — но и менять его тогда незачем.
        """

        tessdata = (
            Path(__file__).resolve().parents[1] / "ocr" / "tessdata"
        )

        self.assertTrue(tessdata.is_dir())
        self.assertFalse(
            (tessdata / "configs").exists(),
            "появился configs/ — проверьте, что вызов Tesseract всё ещё "
            "задаёт виды выдачи переменными",
        )


class FakeRect:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


class FakePixmap:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.samples = bytes(
            np.full(width * height, 255, dtype=np.uint8)
        )


class FakePage:
    """Страница А4 без вложенного растра: dpi возьмётся минимальный."""

    def __init__(self) -> None:
        self.rect = FakeRect(595.0, 842.0)

    def get_text(self, kind: str):
        return {"blocks": []}

    def get_pixmap(self, **kwargs):
        return FakePixmap(600, 850)


class LadderTests(unittest.TestCase):
    """Каждая попытка подменяется: важно, сколько их и какая победила."""

    def setUp(self) -> None:
        self.calls: list[str] = []

    def run_ocr(
        self,
        outputs: list[TesseractOutput],
        **overrides,
    ):
        remaining = list(outputs)

        def fake_recognize(prepared, **kwargs):
            self.calls.append("attempt")

            if remaining:
                return remaining.pop(0)

            return remaining_default

        remaining_default = TesseractOutput(
            text="хвост",
            confidence=0.0,
            low_confidence_ratio=1.0,
            words=1,
        )

        parameters = {
            "binary": "/usr/bin/tesseract",
            "tessdata": Path("/tmp/tessdata"),
            "languages": "rus",
            "minimum_dpi": 200,
            "maximum_dpi": 400,
            "pixel_budget": 8_000_000,
            "psm": 3,
            "oem": 1,
            "threads": 1,
            "timeout_seconds": 45,
            "working_directory": Path("/tmp"),
        }
        parameters.update(overrides)

        # Страница залита белым, поэтому подготовка сочла бы её пустой и
        # вышла бы до первой попытки. Подменяем только признак пустоты.
        original_prepare = pdf_ocr_engine.prepare_page

        def fake_prepare(image, **kwargs):
            prepared = original_prepare(image, **kwargs)
            return prepared.__class__(
                image=Image.new("L", (40, 40), 255),
                skew_degrees=prepared.skew_degrees,
                ink_ratio=0.2,
                blank=False,
                method=prepared.method,
            )

        with patch.object(
            pdf_ocr_engine,
            "_recognize_prepared",
            side_effect=fake_recognize,
        ), patch.object(
            pdf_ocr_engine,
            "prepare_page",
            side_effect=fake_prepare,
        ):
            return ocr_page(FakePage(), **parameters)

    def test_confident_page_costs_one_attempt(self) -> None:
        result = self.run_ocr(
            [
                TesseractOutput(
                    text="АДМИНИСТРАЦИЯ ГОРОДА СОЧИ",
                    confidence=92.0,
                    low_confidence_ratio=0.02,
                    words=3,
                )
            ],
            confidence_floor=65.0,
            retry_rotations=(90, 270, 180),
        )

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result.confidence, 92.0)
        self.assertEqual(result.rotation, 0)
        self.assertEqual(len(result.attempts), 1)

    def test_ladder_is_disabled_without_floor(self) -> None:
        """Нулевой порог — прежнее поведение: одна попытка и всё."""

        self.run_ocr(
            [
                TesseractOutput(
                    text="каша",
                    confidence=11.0,
                    low_confidence_ratio=0.9,
                    words=2,
                )
            ],
            confidence_floor=0.0,
            retry_rotations=(90, 270, 180),
        )

        self.assertEqual(len(self.calls), 1)

    def test_low_confidence_triggers_retries(self) -> None:
        result = self.run_ocr(
            [
                TesseractOutput(
                    text="каша", confidence=19.0,
                    low_confidence_ratio=0.9, words=2,
                ),
                TesseractOutput(
                    text="каша помягче", confidence=24.0,
                    low_confidence_ratio=0.8, words=2,
                ),
                TesseractOutput(
                    text="ПОСТАНОВЛЕНИЕ № 512-р", confidence=91.0,
                    low_confidence_ratio=0.03, words=3,
                ),
            ],
            confidence_floor=65.0,
            retry_rotations=(90, 270, 180),
        )

        # Третья попытка прошла порог — четвёртая не запускается.
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result.text, "ПОСТАНОВЛЕНИЕ № 512-р")
        self.assertEqual(result.confidence, 91.0)
        self.assertEqual(result.rotation, 90)

    def test_best_attempt_wins_when_none_pass(self) -> None:
        result = self.run_ocr(
            [
                TesseractOutput(
                    text="первая", confidence=19.0,
                    low_confidence_ratio=0.9, words=1,
                ),
                TesseractOutput(
                    text="лучшая", confidence=41.0,
                    low_confidence_ratio=0.7, words=1,
                ),
                TesseractOutput(
                    text="третья", confidence=12.0,
                    low_confidence_ratio=0.9, words=1,
                ),
                TesseractOutput(
                    text="четвёртая", confidence=8.0,
                    low_confidence_ratio=0.9, words=1,
                ),
                TesseractOutput(
                    text="пятая", confidence=5.0,
                    low_confidence_ratio=0.9, words=1,
                ),
            ],
            confidence_floor=65.0,
            retry_rotations=(90, 270, 180),
        )

        # Прямо, мягкий порог и три поворота: лестница пройдена целиком.
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(result.text, "лучшая")
        self.assertEqual(result.confidence, 41.0)
        self.assertEqual(len(result.attempts), 5)

    def test_rotation_zero_is_not_retried(self) -> None:
        """Ноль в списке поворотов — это уже сделанная первая попытка."""

        self.run_ocr(
            [
                TesseractOutput(
                    text="каша", confidence=10.0,
                    low_confidence_ratio=0.9, words=1,
                ),
            ],
            confidence_floor=65.0,
            retry_rotations=(0, 180),
            retry_sauvola_k=0.15,
            sauvola_k=0.15,
        )

        # Мягкая бинаризация совпадает с основной и пропускается,
        # остаётся ровно один поворот.
        self.assertEqual(len(self.calls), 2)

    def test_unmeasured_confidence_does_not_trigger_ladder(self) -> None:
        """
        Ноль от неудачного разбора TSV — не признак плохой страницы.

        Ноль меньше любого порога, поэтому лестница шла бы на каждой
        странице, а при равных нулях побеждала бы первая попытка: время
        впятеро, польза нулевая. Отличаем «не измерили» от «измерили и
        плохо» по числу слов.
        """

        result = self.run_ocr(
            [
                TesseractOutput(
                    text="АДМИНИСТРАЦИЯ ГОРОДА СОЧИ ПОСТАНОВЛЕНИЕ",
                    confidence=0.0,
                    low_confidence_ratio=1.0,
                    words=0,
                )
            ],
            confidence_floor=65.0,
            retry_rotations=(90, 270, 180),
        )

        self.assertEqual(len(self.calls), 1)
        self.assertIn("ПОСТАНОВЛЕНИЕ", result.text)

    def test_empty_result_still_triggers_ladder(self) -> None:
        """А вот пустой текст — настоящий повод попробовать иначе."""

        self.run_ocr(
            [
                TesseractOutput(
                    text="",
                    confidence=0.0,
                    low_confidence_ratio=1.0,
                    words=0,
                ),
                TesseractOutput(
                    text="ПОСТАНОВЛЕНИЕ № 512-р",
                    confidence=88.0,
                    low_confidence_ratio=0.02,
                    words=3,
                ),
            ],
            confidence_floor=65.0,
            retry_rotations=(90, 270, 180),
        )

        self.assertGreater(len(self.calls), 1)

    def test_timeout_on_first_attempt_stops_ladder(self) -> None:
        """
        Таймаут — не повод пробовать ещё четыре раза.

        Страница, на которой Tesseract не уложился в отведённое время,
        с большой вероятностью не уложится и в поворотах, а бюджет
        документа один на всех.
        """

        def fake_recognize(prepared, **kwargs):
            self.calls.append("attempt")
            raise pdf_ocr_engine.PageTimeoutError("превышено 45 секунд")

        original_prepare = pdf_ocr_engine.prepare_page

        def fake_prepare(image, **kwargs):
            prepared = original_prepare(image, **kwargs)
            return prepared.__class__(
                image=Image.new("L", (40, 40), 255),
                skew_degrees=0.0,
                ink_ratio=0.2,
                blank=False,
                method="sauvola",
            )

        with patch.object(
            pdf_ocr_engine,
            "_recognize_prepared",
            side_effect=fake_recognize,
        ), patch.object(
            pdf_ocr_engine,
            "prepare_page",
            side_effect=fake_prepare,
        ):
            result = ocr_page(
                FakePage(),
                binary="/usr/bin/tesseract",
                tessdata=Path("/tmp/tessdata"),
                languages="rus",
                minimum_dpi=200,
                maximum_dpi=400,
                pixel_budget=8_000_000,
                psm=3,
                oem=1,
                threads=1,
                timeout_seconds=45,
                working_directory=Path("/tmp"),
                confidence_floor=65.0,
                retry_rotations=(90, 270, 180),
            )

        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.text, "")


if __name__ == "__main__":
    unittest.main()
