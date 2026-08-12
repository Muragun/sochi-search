from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ops.wait_for_elasticsearch import (
    ProbeResult,
    configured_url,
    probe_elasticsearch,
    wait_for_elasticsearch,
)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class RuntimeReliabilityTests(unittest.TestCase):
    def test_probe_requires_yellow_or_green_without_timeout(self) -> None:
        payloads = (
            ({"status": "green", "timed_out": False}, True),
            ({"status": "yellow", "timed_out": False}, True),
            ({"status": "red", "timed_out": True}, False),
        )

        for payload, expected in payloads:
            with self.subTest(payload=payload), patch(
                "ops.wait_for_elasticsearch.urlopen",
                return_value=FakeResponse(payload),
            ):
                result = probe_elasticsearch(
                    "http://127.0.0.1:9200",
                    1.0,
                )
                self.assertEqual(result.ready, expected)

    def test_wait_retries_until_ready(self) -> None:
        clock = FakeClock()
        results = iter(
            (
                ProbeResult(False, "connection refused"),
                ProbeResult(False, "status=red", "red"),
                ProbeResult(True, "status=yellow", "yellow"),
            )
        )

        result = wait_for_elasticsearch(
            base_url="http://127.0.0.1:9200",
            timeout=20.0,
            interval=2.0,
            request_timeout=1.0,
            probe=lambda _url, _timeout: next(results),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertTrue(result.ready)
        self.assertEqual(result.status, "yellow")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(clock.sleeps, [2.0, 2.0])

    def test_wait_stops_at_deadline(self) -> None:
        clock = FakeClock()
        result = wait_for_elasticsearch(
            base_url="http://127.0.0.1:9200",
            timeout=3.0,
            interval=2.0,
            request_timeout=1.0,
            probe=lambda _url, _timeout: ProbeResult(False, "not ready"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(clock.sleeps, [2.0, 1.0])

    def test_url_validation_rejects_non_root_and_bad_scheme(self) -> None:
        self.assertEqual(
            configured_url("http://127.0.0.1:9200/"),
            "http://127.0.0.1:9200",
        )

        for value in (
            "file:///tmp/elasticsearch",
            "http://127.0.0.1:9200/not-root",
            "127.0.0.1:9200",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configured_url(value)

    def test_packaged_dropins_select_html_discovery_and_readiness(self) -> None:
        root = Path(__file__).parents[1]
        discovery = (
            root
            / "systemd"
            / "sochi-search-discovery.service.d"
            / "20-html-discovery.conf"
        ).read_text(encoding="utf-8")

        self.assertIn("ExecStart=", discovery)
        self.assertIn("crawler_v2.discovery_worker_v2", discovery)
        self.assertIn("--html-only", discovery)
        self.assertNotIn("crawler_v2.discovery_worker --", discovery)

        for service in (
            "sochi-search-api-v2.service.d",
            "sochi-search-worker.service.d",
            "sochi-search-publication-date.service.d",
        ):
            dropin = (
                root
                / "systemd"
                / service
                / "20-elasticsearch-ready.conf"
            ).read_text(encoding="utf-8")
            self.assertIn("After=docker.service", dropin)
            self.assertIn("ops.wait_for_elasticsearch", dropin)

            if service == "sochi-search-worker.service.d":
                self.assertIn(
                    "TimeoutStartSec=12h",
                    dropin,
                )
                self.assertIn(
                    "KillMode=control-group",
                    dropin,
                )

        # С 2.5.0 распознавание разложено по слотам: шаблонный юнит `@`
        # вместо одиночного, свой lock на слот, пачка документов за
        # запуск вместо одного файла.
        pdf_service = (
            root
            / "systemd"
            / "sochi-search-pdf-ocr@.service"
        ).read_text(encoding="utf-8")
        pdf_timer = (
            root
            / "systemd"
            / "sochi-search-pdf-ocr@.timer"
        ).read_text(encoding="utf-8")

        for marker in (
            "--pdf-ocr-backfill",
            "--batch",
            "--worker-slot %i",
            "pdf-ocr-worker-%i.lock",
            "Nice=15",
            "IOSchedulingClass=idle",
            "KillMode=control-group",
            "OMP_THREAD_LIMIT=1",
        ):
            self.assertIn(marker, pdf_service)

        # Таймер на каждый слот. Общий `.target` оставался активным после
        # первого запуска, и повторные срабатывания ничего не делали.
        self.assertIn("Unit=sochi-search-pdf-ocr@%i.service", pdf_timer)
        self.assertIn("OnUnitInactiveSec=", pdf_timer)

        compose = (
            root / "docker-compose.app.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("restart: unless-stopped", compose)

    def test_installer_is_not_part_of_this_repository(self) -> None:
        """
        `install.sh` живёт только на рабочем сервере.

        Прежняя версия этого теста читала установщик по пути `../../`,
        то есть за пределами проекта: тест проходил в разложенном
        пакете 2.4.5 и падал в любом клоне репозитория. Установщик
        нужен только для варианта без Docker; контейнерное
        развёртывание делает `deploy.sh`, который лежит рядом.

        Проверяется ровно то, что можно проверить: путь развёртывания
        внутри репозитория существует и делает свою работу.
        """

        root = Path(__file__).parents[1]

        self.assertFalse(
            (root / "install.sh").exists(),
            msg=(
                "install.sh появился в репозитории — тогда его надо "
                "проверять по существу, а не отмечать отсутствие"
            ),
        )

        deploy = (root / "deploy.sh").read_text(encoding="utf-8")

        for marker in (
            "ADMIN_PASSWORD_SHA256",
            "API_BIND",
            "reindex_v3 --bootstrap",
            "vm.max_map_count",
        ):
            self.assertIn(marker, deploy)


if __name__ == "__main__":
    unittest.main()
