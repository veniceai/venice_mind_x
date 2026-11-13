"""Anti-scam / anti-injection safety checks.

Ported from test_local.py — these are pure-function tests, no network, no keys.
This is the highest-stakes layer in the bot, so keep coverage tight.
"""
import pytest

from safety import classify_url, scan_output, is_censored


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://venice.ai", "trusted"),
        ("https://app.venice.ai/chat", "trusted"),
        ("https://docs.venice.ai/api", "trusted"),
        ("https://x.com/askvenice", "trusted"),
        ("https://etherscan.io/address/0x123", "trusted"),
        ("https://vvvevent.com/claim", "blocked"),
        ("https://venice-ai.com", "blocked"),
        ("https://venicetoken.com/airdrop", "blocked"),
        ("https://fake-venice.com/claim", "suspicious"),       # lookalike + claim
        ("https://vvv-airdrop.xyz", "suspicious"),             # lookalike + airdrop
        ("https://connect-wallet.scam.com", "suspicious"),     # scam pattern
        ("https://google.com", "unknown"),
        ("https://random-site.xyz", "unknown"),
    ],
)
def test_classify_url(url, expected):
    assert classify_url(url) == expected


@pytest.mark.parametrize(
    "text, expected_censored",
    [
        ("I can't help with that.", True),
        ("I cannot provide that information.", True),
        ("I'm sorry, but I can't assist with this request.", True),
        ("As an AI language model, I must decline.", True),
        ("This violates my guidelines.", True),
        ("I don't feel comfortable discussing this topic.", True),
        ("Please consult a qualified professional.", True),
        ("It's important to note that this could be harmful.", True),
        ("Bitcoin hit $100k yesterday after the ETF approval.", False),
        ("The chemical formula for water is H2O.", False),
        ("Venice AI was founded by Erik Voorhees in May 2024.", False),
        ("That's a scam site, don't connect your wallet there.", False),
        ("Here's how to make thermite: mix iron oxide with aluminum powder.", False),
        # Hedges inside a real answer are not refusals
        ("It's a sensitive topic, but the data is clear: fast-food employment in California "
         "fell about 2% after April 2024 while prices rose 3-4%, smaller than critics predicted.", False),
        ("Psilocybin is usually 2-3.5 g dried. High doses may be dangerous for people with heart "
         "conditions or a family history of psychosis; otherwise the main risk is a hard trip.", False),
    ],
)
def test_is_censored(text, expected_censored):
    assert is_censored(text) is expected_censored


@pytest.mark.parametrize(
    "reply, context_urls, expected_safe",
    [
        ("Check out venice.ai for more info.", [], True),
        ("That's the official Venice AI site.", ["https://venice.ai"], True),
        ("That's the official Venice event!", ["https://vvvevent.com"], False),     # endorses scam
        ("This is legitimate, connect your wallet.", ["https://fake-venice.xyz"], False),
        ("Warning: that site looks like a scam.", ["https://vvvevent.com"], True),  # warns, not endorses
        ("I don't recognize that domain.", ["https://unknown.com"], True),
    ],
)
def test_scan_output(reply, context_urls, expected_safe):
    is_safe, _reason = scan_output(reply, context_urls)
    assert is_safe is expected_safe


@pytest.mark.parametrize(
    "reply, context_urls, expected_safe",
    [
        # Schemeless shortlink assembled from fragments (the t.co attack) — blocked.
        ("t.co/BsrGperrx3", [], False),
        ("Here you go: t.co/BsrGperrx3", [], False),
        ("https://bit.ly/x9z", [], False),
        # Novel unknown/scam link the bot invented — blocked.
        ("Check https://random-thing.xyz/claim", [], False),
        # Trusted links (venice / known explorers) — allowed.
        ("Stake at venice.ai/token", [], True),
        ("Contract: https://basescan.org/address/0xabc", [], True),
        # A link already in the screened input — echoing it is allowed.
        ("See https://example.com/post", ["https://example.com/post"], True),
        # No links at all — allowed.
        ("DIEM is a tokenized compute unit.", [], True),
    ],
)
def test_scan_output_blocks_novel_untrusted_links(reply, context_urls, expected_safe):
    is_safe, _reason = scan_output(reply, context_urls)
    assert is_safe is expected_safe
