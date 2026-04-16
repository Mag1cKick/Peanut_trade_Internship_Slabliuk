"""
tests/test_arb_logger.py — Unit tests for integration.arb_logger.ArbLogger

All tests use a stub checker — no real exchange connections.

Test groups:
  1. Construction — stores checker, creates CSV header
  2. check() — calls checker, logs result, returns result unchanged
  3. log_result() — logs pre-computed result
  4. recent() — returns last n entries
  5. stats() — aggregate metrics
  6. export_csv() — writes correct CSV file
  7. flush() — clears buffer, preserves total_logged
  8. CSV file output — header, rows, append behaviour
  9. _result_to_row helper
 10. CLI smoke test
"""

from __future__ import annotations

import csv
import os
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from integration.arb_logger import _CSV_FIELDS, ArbLogger, _result_to_row

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_result(
    pair: str = "ETH/USDT",
    direction: str | None = "buy_dex_sell_cex",
    net_pnl_bps: float = 5.0,
    executable: bool = True,
    inventory_ok: bool = True,
) -> dict:
    return {
        "pair": pair,
        "timestamp": datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC),
        "dex_price": Decimal("2000"),
        "cex_bid": Decimal("2010"),
        "cex_ask": Decimal("2011"),
        "gap_bps": Decimal("50"),
        "direction": direction,
        "estimated_costs_bps": Decimal(str(50 - net_pnl_bps)),
        "estimated_net_pnl_bps": Decimal(str(net_pnl_bps)),
        "inventory_ok": inventory_ok,
        "executable": executable,
        "details": {
            "dex_fee_bps": Decimal("30"),
            "dex_price_impact_bps": Decimal("1.2"),
            "cex_fee_bps": Decimal("10"),
            "cex_slippage_bps": Decimal("2"),
            "gas_cost_usd": Decimal("0.5"),
        },
    }


def _make_checker(result: dict | None = None) -> MagicMock:
    checker = MagicMock()
    checker.check.return_value = result or _make_result()
    return checker


def _make_logger(result: dict | None = None, csv_path: str | None = None) -> ArbLogger:
    return ArbLogger(_make_checker(result), csv_path=csv_path, maxlen=50)


# ── 1. Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_stores_checker(self):
        checker = _make_checker()
        logger = ArbLogger(checker)
        assert logger._checker is checker

    def test_default_maxlen(self):
        logger = _make_logger()
        assert logger._buffer.maxlen == 50

    def test_custom_maxlen(self):
        logger = ArbLogger(_make_checker(), maxlen=100)
        assert logger._buffer.maxlen == 100

    def test_total_logged_starts_zero(self):
        logger = _make_logger()
        assert logger._total_logged == 0

    def test_creates_csv_header_on_init(self, tmp_path):
        path = str(tmp_path / "log.csv")
        ArbLogger(_make_checker(), csv_path=path)
        assert os.path.exists(path)
        with open(path) as f:
            header = f.readline().strip()
        assert "pair" in header
        assert "executable" in header

    def test_no_csv_created_when_path_is_none(self, tmp_path):
        initial = set(os.listdir(tmp_path))
        ArbLogger(_make_checker(), csv_path=None)
        assert set(os.listdir(tmp_path)) == initial


# ── 2. check() ────────────────────────────────────────────────────────────────


class TestCheck:
    def test_calls_checker_check(self):
        checker = _make_checker()
        logger = ArbLogger(checker)
        logger.check("ETH/USDT")
        checker.check.assert_called_once()

    def test_passes_pair_to_checker(self):
        checker = _make_checker()
        logger = ArbLogger(checker)
        logger.check("BTC/USDT", size=0.1)
        args = checker.check.call_args
        assert args[0][0] == "BTC/USDT"

    def test_returns_result_unchanged(self):
        result = _make_result()
        logger = _make_logger(result)
        returned = logger.check("ETH/USDT")
        assert returned is result

    def test_increments_total_logged(self):
        logger = _make_logger()
        logger.check("ETH/USDT")
        assert logger._total_logged == 1

    def test_buffer_grows(self):
        logger = _make_logger()
        logger.check("ETH/USDT")
        logger.check("ETH/USDT")
        assert len(logger._buffer) == 2

    def test_appends_to_csv(self, tmp_path):
        path = str(tmp_path / "log.csv")
        logger = ArbLogger(_make_checker(), csv_path=path)
        logger.check("ETH/USDT")
        with open(path) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["pair"] == "ETH/USDT"


# ── 3. log_result() ───────────────────────────────────────────────────────────


class TestLogResult:
    def test_logs_without_calling_checker(self):
        checker = _make_checker()
        logger = ArbLogger(checker)
        result = _make_result()
        logger.log_result(result)
        checker.check.assert_not_called()

    def test_increments_total_logged(self):
        logger = _make_logger()
        logger.log_result(_make_result())
        assert logger._total_logged == 1

    def test_buffer_grows(self):
        logger = _make_logger()
        logger.log_result(_make_result())
        assert len(logger._buffer) == 1

    def test_note_stored(self):
        logger = _make_logger()
        logger.log_result(_make_result(), note="test_note")
        assert logger._buffer[-1]["note"] == "test_note"


# ── 4. recent() ───────────────────────────────────────────────────────────────


class TestRecent:
    def test_returns_list(self):
        logger = _make_logger()
        assert isinstance(logger.recent(), list)

    def test_empty_when_no_logs(self):
        logger = _make_logger()
        assert logger.recent() == []

    def test_returns_last_n(self):
        logger = _make_logger()
        for i in range(5):
            logger.log_result(_make_result(net_pnl_bps=float(i)))
        recent = logger.recent(3)
        assert len(recent) == 3

    def test_newest_is_last(self):
        logger = _make_logger()
        for i in range(3):
            logger.log_result(_make_result(net_pnl_bps=float(i)))
        recent = logger.recent(3)
        # last entry should have the highest net_pnl_bps value
        assert float(recent[-1]["estimated_net_pnl_bps"]) == 2.0

    def test_returns_all_when_n_exceeds_buffer(self):
        logger = _make_logger()
        logger.log_result(_make_result())
        assert len(logger.recent(100)) == 1


# ── 5. stats() ────────────────────────────────────────────────────────────────


class TestStats:
    def test_empty_buffer(self):
        stats = _make_logger().stats()
        assert stats["total_logged"] == 0
        assert stats["buffer_size"] == 0
        assert stats["executable_count"] == 0

    def test_total_logged_correct(self):
        logger = _make_logger()
        logger.log_result(_make_result())
        logger.log_result(_make_result())
        assert logger.stats()["total_logged"] == 2

    def test_executable_count(self):
        logger = _make_logger()
        logger.log_result(_make_result(executable=True))
        logger.log_result(_make_result(executable=False))
        logger.log_result(_make_result(executable=True))
        stats = logger.stats()
        assert stats["executable_count"] == 2

    def test_executable_pct(self):
        logger = _make_logger()
        logger.log_result(_make_result(executable=True))
        logger.log_result(_make_result(executable=False))
        stats = logger.stats()
        assert abs(stats["executable_pct"] - 0.5) < 1e-9

    def test_avg_net_pnl_bps(self):
        logger = _make_logger()
        logger.log_result(_make_result(net_pnl_bps=10.0))
        logger.log_result(_make_result(net_pnl_bps=20.0))
        stats = logger.stats()
        assert abs(stats["avg_net_pnl_bps"] - 15.0) < 1e-6

    def test_pairs_list(self):
        logger = _make_logger()
        logger.log_result(_make_result(pair="ETH/USDT"))
        logger.log_result(_make_result(pair="BTC/USDT"))
        stats = logger.stats()
        assert "ETH/USDT" in stats["pairs"]
        assert "BTC/USDT" in stats["pairs"]


# ── 6. export_csv() ───────────────────────────────────────────────────────────


class TestExportCsv:
    def test_creates_file(self, tmp_path):
        path = str(tmp_path / "export.csv")
        logger = _make_logger()
        logger.log_result(_make_result())
        logger.export_csv(path)
        assert os.path.exists(path)

    def test_returns_row_count(self, tmp_path):
        path = str(tmp_path / "export.csv")
        logger = _make_logger()
        for _ in range(3):
            logger.log_result(_make_result())
        n = logger.export_csv(path)
        assert n == 3

    def test_csv_has_header(self, tmp_path):
        path = str(tmp_path / "export.csv")
        logger = _make_logger()
        logger.log_result(_make_result())
        logger.export_csv(path)
        with open(path) as f:
            header = f.readline().strip().split(",")
        assert "pair" in header
        assert "executable" in header

    def test_csv_has_all_fields(self, tmp_path):
        path = str(tmp_path / "export.csv")
        logger = _make_logger()
        logger.log_result(_make_result())
        logger.export_csv(path)
        with open(path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        for field in _CSV_FIELDS:
            assert field in rows[0]

    def test_empty_buffer_writes_header_only(self, tmp_path):
        path = str(tmp_path / "empty.csv")
        logger = _make_logger()
        n = logger.export_csv(path)
        assert n == 0
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 1  # header only


# ── 7. flush() ────────────────────────────────────────────────────────────────


class TestFlush:
    def test_clears_buffer(self):
        logger = _make_logger()
        logger.log_result(_make_result())
        logger.flush()
        assert len(logger._buffer) == 0

    def test_total_logged_preserved_after_flush(self):
        logger = _make_logger()
        logger.log_result(_make_result())
        logger.log_result(_make_result())
        logger.flush()
        assert logger._total_logged == 2

    def test_stats_after_flush(self):
        logger = _make_logger()
        logger.log_result(_make_result())
        logger.flush()
        stats = logger.stats()
        assert stats["buffer_size"] == 0
        assert stats["total_logged"] == 1


# ── 8. CSV append behaviour ───────────────────────────────────────────────────


class TestCsvAppend:
    def test_multiple_checks_append_rows(self, tmp_path):
        path = str(tmp_path / "append.csv")
        logger = ArbLogger(_make_checker(), csv_path=path)
        logger.check("ETH/USDT")
        logger.check("ETH/USDT")
        with open(path) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2

    def test_ring_buffer_limits_memory(self):
        logger = ArbLogger(_make_checker(), maxlen=3)
        for _ in range(10):
            logger.log_result(_make_result())
        assert len(logger._buffer) == 3
        assert logger._total_logged == 10


# ── 9. _result_to_row helper ──────────────────────────────────────────────────


class TestResultToRow:
    def test_all_csv_fields_present(self):
        row = _result_to_row(_make_result())
        for field in _CSV_FIELDS:
            assert field in row

    def test_pair_correct(self):
        row = _result_to_row(_make_result(pair="BTC/USDT"))
        assert row["pair"] == "BTC/USDT"

    def test_executable_serialised_as_string(self):
        row = _result_to_row(_make_result(executable=True))
        assert row["executable"] == "True"

    def test_no_direction_becomes_empty_string(self):
        row = _result_to_row(_make_result(direction=None))
        assert row["direction"] == ""

    def test_note_preserved(self):
        row = _result_to_row(_make_result(), note="hello")
        assert row["note"] == "hello"


# ── 10. CLI smoke ──────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_module_importable(self):
        from integration.arb_logger import _run_cli

        assert callable(_run_cli)


# ── Coverage gap tests ─────────────────────────────────────────────────────────


class TestStatsKeyErrorPath:
    """Cover except (KeyError, ValueError): pass in stats() (lines 220-221)."""

    def test_malformed_entry_skipped_in_avg(self):
        logger = _make_logger()
        # Manually insert a malformed entry missing estimated_net_pnl_bps
        logger._buffer.append({"pair": "ETH/USDT", "executable": "True"})
        logger._total_logged += 1
        stats = logger.stats()
        # Should not raise — just skip the malformed entry
        assert stats["buffer_size"] == 1
        assert stats["avg_net_pnl_bps"] == 0.0


class TestRunCLI:
    """_run_cli with mocked ExchangeClient."""

    def _mock_cex(self):
        mock = MagicMock()
        mock.fetch_order_book.return_value = {
            "bids": [(Decimal("2000"), Decimal("5"))],
            "asks": [(Decimal("2001"), Decimal("5"))],
            "best_bid": (Decimal("2000"), Decimal("5")),
            "best_ask": (Decimal("2001"), Decimal("5")),
            "mid_price": Decimal("2000.5"),
            "spread_bps": Decimal("5"),
            "symbol": "ETH/USDT",
            "timestamp": 1700000000000,
        }
        mock.get_trading_fees.return_value = {"taker": Decimal("0.001"), "maker": Decimal("0.001")}
        return mock

    def test_cli_exits_zero(self, capsys):
        from integration.arb_logger import _run_cli

        with patch("exchange.client.ExchangeClient", return_value=self._mock_cex()):
            rc = _run_cli(["ETH/USDT", "--size", "1.0"])
        assert rc == 0

    def test_cli_with_export(self, tmp_path, capsys):
        from integration.arb_logger import _run_cli

        out = str(tmp_path / "log.csv")
        with patch("exchange.client.ExchangeClient", return_value=self._mock_cex()):
            rc = _run_cli(["ETH/USDT", "--size", "1.0", "--export", out])
        assert rc == 0
        assert os.path.exists(out)

    def test_cli_exchange_error_returns_one(self):
        from integration.arb_logger import _run_cli

        with patch("exchange.client.ExchangeClient", side_effect=Exception("fail")):
            rc = _run_cli(["ETH/USDT"])
        assert rc == 1

    def test_cli_order_book_error_returns_one(self):
        from integration.arb_logger import _run_cli

        mock_cex = self._mock_cex()
        mock_cex.fetch_order_book.side_effect = Exception("book error")
        with patch("exchange.client.ExchangeClient", return_value=mock_cex):
            rc = _run_cli(["ETH/USDT"])
        assert rc == 1

    def test_cli_with_dex_price_and_note(self, capsys):
        from integration.arb_logger import _run_cli

        with patch("exchange.client.ExchangeClient", return_value=self._mock_cex()):
            rc = _run_cli(["ETH/USDT", "--dex-price", "1990", "--note", "test"])
        assert rc == 0
