"""CLI smoke tests."""

from __future__ import annotations

import json

from quant_system.cli import main


def test_cli_halt_resume_status(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "halt", "--reason", "cli-test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["halted"] is True

    assert main(["--db", db, "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["halted"] is True

    assert main(["--db", db, "resume"]) == 0
    capsys.readouterr()  # discard resume output
    assert main(["--db", db, "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["halted"] is False


def test_cli_run_once_mock_and_digest(tmp_path, capsys):
    db = str(tmp_path / "cli2.db")
    rc = main(["--db", db, "run-once", "--mock", "--tickers", "IONQ"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["signals"] >= 5
    assert main(["--db", db, "digest", "--limit", "5"]) == 0
    dig = json.loads(capsys.readouterr().out)
    assert dig["recent_signals"]
