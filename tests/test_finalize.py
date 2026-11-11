"""The finalize step: X rules, token-returns backstop, gate, one rewrite."""
import finalize


def _wire(monkeypatch, gate_results, rewrites):
    gate, rew = list(gate_results), list(rewrites)
    calls = {"gate": [], "rewrite": []}

    def crossed_line(text, thread, sources):
        calls["gate"].append((text, thread, sources))
        return gate.pop(0) if gate else None

    def rewrite_reply(text, failed):
        calls["rewrite"].append((text, failed))
        if failed not in ("unsupported", "token_returns"):
            return None
        return rew.pop(0) if rew else None

    monkeypatch.setattr(finalize, "crossed_line", crossed_line)
    monkeypatch.setattr(finalize, "rewrite_reply", rewrite_reply)
    return calls


def test_clean_reply_posts_after_x_rules(monkeypatch):
    calls = _wire(monkeypatch, [None], [])
    out, failed = finalize.finalize_reply("$VVV and $DIEM, see https://x.co/a", thread="T", sources="S")
    assert (out, failed) == ("$VVV and DIEM, see", None)
    assert calls["gate"][0][1:] == ("T", "S")
    assert calls["rewrite"] == []


def test_unsupported_claim_is_rewritten_once_then_posts(monkeypatch):
    calls = _wire(monkeypatch, ["unsupported", None], ["Unverified: no outlet reports it."])
    out, failed = finalize.finalize_reply("Yes, he was hospitalized last week.")
    assert (out, failed) == ("Unverified: no outlet reports it.", None)
    assert calls["rewrite"] == [("Yes, he was hospitalized last week.", "unsupported")]


def test_still_failing_after_the_rewrite_posts_nothing(monkeypatch):
    _wire(monkeypatch, ["unsupported", "unsupported"], ["Still a claim."])
    assert finalize.finalize_reply("A claim.") == (None, "unsupported")


def test_hard_lines_are_never_rewritten(monkeypatch):
    _wire(monkeypatch, ["private_info"], ["anything"])
    out, failed = finalize.finalize_reply("He lives at 12 Elm St.")
    assert (out, failed) == (None, "private_info")


def test_token_returns_regex_runs_even_if_the_gate_would_pass(monkeypatch):
    calls = _wire(monkeypatch, [None, None], ["DIEM gives $1/day of API credit."])
    out, failed = finalize.finalize_reply("DIEM yields ~21% a year.")
    assert (out, failed) == ("DIEM gives $1/day of API credit.", None)
    assert calls["rewrite"][0][1] == "token_returns"
    assert len(calls["gate"]) == 1      # the regex caught the first draft before the gate
