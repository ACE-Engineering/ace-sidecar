"""The dashboard against the overhaul design (`ace-sidecar-overhaul`).

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
#: The AceFleet logo's own colours. Exempt from the palette guard on purpose: the mark is
#: artwork, not UI, and a logo that recoloured itself with the theme would stop being the
#: logo. `branding.py` says the same thing from the other side.
BRAND = {"#D9FF3F", "#7EDCFF", "#86A700", "#168DB3"}

PALETTE = {
    # light — the prototype's own surfaces and fills, verbatim
    "#f4f3ed", "#ebeae1", "#fbfaf5", "#eaeae0", "#ffffff",
    "#1f211c", "#5d6258", "#deded2", "#cbcdbb",
    "#a9ba53", "#e7edbd", "#edf2d2", "#819538",
    "#77acb1", "#9b8dd2", "#d29e64", "#e9f1ef", "#f0edf8", "#f8eee1",
    # light — small-text values darkened to clear 4.5:1 (see the stylesheet's note 1)
    "#676B60", "#626F2B", "#426F74", "#6D58BD", "#8E5F29", "#9E2B2B",
    # dark — NEUTRAL charcoal surfaces. Every one is R>=G>B; a green channel above red here
    # is the bug that made the whole page read olive instead of dark, so the ordering is the
    # invariant, not the exact values.
    "#131312", "#0E0E0D", "#1A1A18", "#0F0F0E", "#222220",
    "#EDEDE8", "#A8A8A0", "#8A8A82", "#262624", "#393936",
    "#BFD45F", "#39401F", "#232519", "#A8BE4E",
    "#8CC4CA", "#B0A2E0", "#DCA86A", "#16261F", "#1E1A2C", "#2A2015", "#F08A8A",
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
    extra = literals - PALETTE - BRAND
    assert not extra, f"off-token colours in page: {sorted(extra)}"


def test_the_superseded_palette_is_gone(client):
    """Both superseded palettes: the original control-plane mint AND the Signal Lime system
    that briefly replaced it. A survivor from either is invisible in review and wrong on
    screen, because each one means the same thing as the olive that now stands in its place."""
    html = client.get("/dashboard").text
    for stale in ("#3ECF8E", "#0A0B0C", "#F2F4F3", "#6C7572", "#212627", "#3987e5", "#090A0D"):
        assert stale not in html, f"{stale} survived the port"
    # Signal Lime and its cyan are NOT listed above: they are still legitimately present, in
    # the logo. What must be gone is their use as UI, which the palette guard above enforces.


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
    assert "outline:2px solid var(--olive-text)" in html
    assert "prefers-reduced-motion" in html


# -- theme ------------------------------------------------------------------------------
#
# Three states, and `auto` is the one worth testing hardest: it is the default, it is the
# only one that defers to the reader's own system, and it is expressed by the ABSENCE of an
# attribute — so the failure mode is silent. An emitted `data-theme="auto"` still matches
# `:root:not([data-theme="dark"])`, so the pinned light rules would win over the media query
# and every reader on a dark OS would get a light dashboard that never changes.


def test_auto_emits_no_attribute_at_all(client):
    html = client.get("/dashboard").text
    assert "<html lang='en'>" in html
    assert "data-theme" not in html.split("<head>")[0]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_pinned_theme_is_stamped_on_the_root(client, theme):
    html = client.get(f"/dashboard?theme={theme}").text
    assert f"data-theme='{theme}'" in html.split("<head>")[0]


def test_an_unknown_theme_falls_back_to_auto_rather_than_erroring(client):
    """A hand-typed URL is not a 500. The dashboard is a page someone reads, not an API."""
    r = client.get("/dashboard?theme=solarized")
    assert r.status_code == 200
    assert "data-theme" not in r.text.split("<head>")[0]


def test_the_choice_survives_the_meta_refresh(client):
    """The page reloads itself every 20s to a bare URL, so the preference cannot live in the
    query string alone — this is the regression that would make the toggle look broken."""
    c = TestClient(build_sidecar_app(api_key="k", base_url="http://127.0.0.1:1"))
    assert c.get("/dashboard?theme=light").cookies.get("ace_theme") == "light"
    # ...and a later request carrying no parameter still renders light.
    assert "data-theme='light'" in c.get("/dashboard").text.split("<head>")[0]


def test_returning_to_auto_clears_the_cookie(client):
    """Otherwise `auto` is unreachable once either theme has been picked: the cookie would
    keep answering for a preference the reader has explicitly withdrawn."""
    c = TestClient(build_sidecar_app(api_key="k", base_url="http://127.0.0.1:1"))
    c.get("/dashboard?theme=dark")
    c.get("/dashboard?theme=auto")
    assert not c.cookies.get("ace_theme")
    assert "data-theme" not in c.get("/dashboard").text.split("<head>")[0]


def test_both_themes_are_defined_and_the_light_one_is_reachable_two_ways(client):
    """Pinned via the attribute, and unpinned via the media query. The overhaul is light-first,
    so it is the DARK block that must exist both ways — defined only under `[data-theme=dark]`
    it would leave an auto reader on a dark OS staring at a white page."""
    html = client.get("/dashboard").text
    assert ':root[data-theme="dark"]{' in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert 'prefers-color-scheme: dark){:root:not([data-theme="light"])' in html


def test_the_switch_offers_all_three_states(client):
    html = client.get("/dashboard").text
    for t in ("auto", "light", "dark"):
        assert f"href='?theme={t}'" in html


def test_dark_surfaces_are_never_green():
    """The regression this exists for: the first dark palette tinted the SURFACES olive —
    `#14160F`, `#1B1E14`, `#101208` — and each had a green channel above its red, so the page
    read as olive rather than as dark. The accent is where the colour belongs; the ground is
    charcoal. Asserting the channel ORDER rather than exact values, because the values are
    allowed to be retuned and the ordering is not.
    """
    import re

    src = __import__("ace.sidecar.dashboard_render", fromlist=["_CSS"])._CSS
    dark = src[src.index(':root[data-theme="dark"]{'):]
    dark = dark[: dark.index("}")]
    surfaces = dict(re.findall(r"--(paper|paper-deep|card|rail|elevated|line|line-dark):(#[0-9A-Fa-f]{6})", dark))
    assert surfaces, "no dark surface tokens found"
    for name, hexv in surfaces.items():
        r, g, b = (int(hexv[i : i + 2], 16) for i in (1, 3, 5))
        assert r >= g >= b, f"--{name} {hexv} is tinted (R{r} G{g} B{b}); dark surfaces must be R>=G>B"
