"""Shared ACE brand assets for the HTML pages the gateway and agent serve.

One definition so every locally served page (index, dashboards, raw logs, DB console,
agent status) shows the same tab icon instead of the browser's default globe.
"""

# ACE brand mark: sharp mint square inside a dark frame — the same mark the dashboard
# mastheads draw in CSS, and the one shipped as ace-fleet's public/favicon.svg.
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect x='2' y='2' width='28' height='28' fill='#0d1117' "
    "stroke='#5b6169' stroke-width='2'/>"
    "<rect x='10' y='10' width='12' height='12' fill='#3ECF8E'/>"
    "</svg>"
)

# Percent-encoded data URI. Inlined rather than served from a route so the pages stay
# self-contained (they are single string literals with no static-file mount).
FAVICON_LINK = (
    "<link rel='icon' href=\"data:image/svg+xml,"
    "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2032%2032'%3E"
    "%3Crect%20x='2'%20y='2'%20width='28'%20height='28'%20"
    "fill='%230d1117'%20stroke='%235b6169'%20stroke-width='2'/%3E"
    "%3Crect%20x='10'%20y='10'%20width='12'%20height='12'%20fill='%233ECF8E'/%3E"
    '%3C/svg%3E">'
)
