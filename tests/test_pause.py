"""The pause file: an operator can stop the bot reading mentions without a restart."""
import pytest

import bot
from config import Config
from state import State


def test_not_paused_without_the_file(tmp_path):
    assert bot.is_paused(str(tmp_path / "PAUSED")) is False


def test_the_pause_file_pauses(tmp_path):
    pause = tmp_path / "PAUSED"
    pause.write_text("")
    assert bot.is_paused(str(pause)) is True


def test_a_paused_loop_reads_no_mentions(monkeypatch, tmp_path):
    pause = tmp_path / "PAUSED"
    pause.write_text("")
    monkeypatch.setattr(Config, "PAUSE_FILE", str(pause))

    instance = object.__new__(bot.VeniceBot)
    instance.state = State()
    instance.state.save = lambda: None
    instance.process_mentions = lambda: pytest.fail("a paused bot must not read mentions")

    def stop(_seconds):
        raise KeyboardInterrupt  # run() exits cleanly on Ctrl-C

    monkeypatch.setattr(bot.time, "sleep", stop)
    instance.run()
