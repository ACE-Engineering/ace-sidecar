"""The dashboard against the AceFleet design system (landing page redesign v2).

These are invariants, not a screenshot diff. What can actually regress here is not "does it
look right" but three things a reader of the diff cannot check by eye: that the page still
routes every colour through the token layer, that the fonts come off loopback rather than
Google, and that the font route cannot be walked out of.
"""

from __future__ import annotations

import re

import pytest

from ace.sidecar.app import build_sidecar_app

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

FONTS = (
    "SpaceGrotesk-var.woff2",
    "DMSans-var.woff2",
    "DMMono-400.woff2",
    "DMMono-500.woff2",
)

# The `--af-` primitives, which are the only literals allowed to survive in the page: every
# other colour has to reach them through a variable. Values are the token file's, verbatim.
AF_PRIMITIVES = {
    "#07080A", "#090A0D", "#0D0E11", "#121419",   # ink ramp
    "#F1EFE9",                                     # paper
    "#D9FF3F", "#7EDCFF", "#AA8AFF", "#FFCE6B",    # signals
    "#FF7A84", "#6D737B",                          # error / off
}


@pytest.fixture(scope="module")
def client():
    return TestClient(build_sidecar_app(api_key="k", base_url="http://127.0.0.1:1"))


def test_every_colour_in_the_page_resolves_through_a_token(client):
    """The one invariant worth enforcing. A hand-rolled hex anywhere in 3,700 lines is how a
    design system rots: it looks right the day it is written and drifts the first time a
    token moves. Anything not in the `:root` block has to be a `var()`."""
    html = client.get("/dashboard").text
    literals = set(re.findall(r"#[0-9A-Fa-f]{6}", html))
    assert literals <= AF_PRIMITIVES, f"off-token colours in page: {sorted(literals - AF_PRIMITIVES)}"


def test_the_superseded_palette_is_gone(client):
    """The v1 control-plane colours. Mint in particular: `#3ECF8E` and Signal Lime mean the
    same thing, so a surviving mint is invisible in review and wrong on screen."""
    html = client.get("/dashboard").text
    for stale in ("#3ECF8E", "#0A0B0C", "#F2F4F3", "#6C7572", "#212627", "#3987e5", "#D8A33C"):
        assert stale not in html, f"{stale} survived the port"


def test_fonts_are_declared_and_served_from_loopback(client):
    """The sidecar's claim is that nothing leaves the machine. acefleet.dev links
    fonts.googleapis.com; if that link ever gets copied in here, every dashboard load
    announces to Google when a developer was working."""
    html = client.get("/dashboard").text
    assert html.count("@font-face") == len(FONTS)
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    for family in ("Space Grotesk", "DM Sans", "DM Mono"):
        assert family in html


@pytest.mark.parametrize("name", FONTS)
def test_each_vendored_face_is_reachable(client, name):
    r = client.get(f"/static/fonts/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "font/woff2"
    assert r.content[:4] == b"wOF2", "not a woff2 payload"
    # The page meta-refreshes every 20s; without immutable caching that is four font
    # requests a minute, forever.
    assert "immutable" in r.headers.get("cache-control", "")


@pytest.mark.parametrize(
    "name",
    ["../app.py", "../../__init__.py", "....//app.py", "nope.woff2", "", "SpaceGrotesk-var.woff2.bak"],
)
def test_the_font_route_serves_nothing_off_the_allowlist(client, name):
    """An allowlist rather than a sanitiser: a sanitiser is a thing to get wrong, and a dict
    lookup cannot be walked out of whatever the path separator of the day turns out to be."""
    assert client.get(f"/static/fonts/{name}").status_code in (404, 405)


def test_accessibility_tokens_are_present(client):
    """Both are in the design system's own spec rather than added here: a 2px lime focus ring
    at 3px offset, and every nonessential animation reducible."""
    html = client.get("/dashboard").text
    assert ":focus-visible" in html
    assert "outline:2px solid var(--af-signal-lime)" in html
    assert "prefers-reduced-motion" in html
