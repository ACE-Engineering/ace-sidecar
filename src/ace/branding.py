"""Shared ACE brand assets for the HTML pages the gateway and agent serve.

One definition so every locally served page (index, dashboards, raw logs, DB console,
agent status) shows the same tab icon instead of the browser's default globe.
"""

# The AceFleet mark: two stacked blades, lime over cyan. Taken from the shipped artwork in
# `ace-fleet/public/favicon.svg` and `acefleet-mark.svg` rather than redrawn. What it replaced
# was a square-with-a-core that predated the brand entirely and was still painted in `#3ECF8E`
# — a mint from two palettes ago — long after nothing else on the page was.
#
# The light and dark icons are NOT recolours of each other. Both keep the same bright fills,
# because the blades are the logo and a logo does not change colour with a theme; only the
# STROKE differs. On `#0B0E13` each blade is its own outline, while on warm paper `#D9FF3F`
# has nothing to sit against, so the light variant outlines each blade in a darker cut of its
# own hue. That is the upstream file's solution, not an invention here — see
# `ace-fleet/public/favicon-light.svg`.

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="none"><rect width="64" height="64" rx="14" fill="#0B0E13"/><g stroke-linejoin="round"><path d="M8.5 20.5 38.8 9.1l14.6 10.5-30.4 11.7L8.5 20.5Z" fill="#D9FF3F" stroke="#D9FF3F" stroke-width="1.2"/><path d="m21.2 34.4 30.4-11.7 14 10.6-30.5 11.8-13.9-10.7Z" fill="#7EDCFF" stroke="#7EDCFF" stroke-width="1.2"/></g><path d="M12 53h40" stroke="#D9FF3F" stroke-opacity=".22"/></svg>'
)

FAVICON_SVG_LIGHT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="none"><rect width="64" height="64" rx="14" fill="#F4F1E9"/><g stroke-linejoin="round"><path d="M8.5 20.5 38.8 9.1l14.6 10.5-30.4 11.7L8.5 20.5Z" fill="#D9FF3F" stroke="#597600" stroke-width="1.2"/><path d="m21.2 34.4 30.4-11.7 14 10.6-30.5 11.8-13.9-10.7Z" fill="#7EDCFF" stroke="#0A7D9C" stroke-width="1.2"/></g><path d="M12 53h40" stroke="#11151B" stroke-opacity=".18"/></svg>'
)

#: The bare mark, no ground and no baseline, for the rail lockup — where the panel behind it
#: already supplies the surface and a second rounded rectangle would read as a button.
BRAND_MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none" width="22" height="22" aria-hidden="true"><g stroke-linejoin="round"><path d="M8.5 20.5 38.8 9.1l14.6 10.5-30.4 11.7L8.5 20.5Z" fill="#D9FF3F" stroke="#86A700" stroke-width="1.2"/><path d="m21.2 34.4 30.4-11.7 14 10.6-30.5 11.8-13.9-10.7Z" fill="#7EDCFF" stroke="#168DB3" stroke-width="1.2"/></g></svg>'
)

# Percent-encoded data URIs. Inlined rather than served from a route so the pages stay
# self-contained (they are single string literals with no static-file mount).
_DARK_URI = "data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2264%22%20height%3D%2264%22%20viewBox%3D%220%200%2064%2064%22%20fill%3D%22none%22%3E%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2214%22%20fill%3D%22%230B0E13%22%2F%3E%3Cg%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M8.5%2020.5%2038.8%209.1l14.6%2010.5-30.4%2011.7L8.5%2020.5Z%22%20fill%3D%22%23D9FF3F%22%20stroke%3D%22%23D9FF3F%22%20stroke-width%3D%221.2%22%2F%3E%3Cpath%20d%3D%22m21.2%2034.4%2030.4-11.7%2014%2010.6-30.5%2011.8-13.9-10.7Z%22%20fill%3D%22%237EDCFF%22%20stroke%3D%22%237EDCFF%22%20stroke-width%3D%221.2%22%2F%3E%3C%2Fg%3E%3Cpath%20d%3D%22M12%2053h40%22%20stroke%3D%22%23D9FF3F%22%20stroke-opacity%3D%22.22%22%2F%3E%3C%2Fsvg%3E"
_LIGHT_URI = "data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2264%22%20height%3D%2264%22%20viewBox%3D%220%200%2064%2064%22%20fill%3D%22none%22%3E%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2214%22%20fill%3D%22%23F4F1E9%22%2F%3E%3Cg%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M8.5%2020.5%2038.8%209.1l14.6%2010.5-30.4%2011.7L8.5%2020.5Z%22%20fill%3D%22%23D9FF3F%22%20stroke%3D%22%23597600%22%20stroke-width%3D%221.2%22%2F%3E%3Cpath%20d%3D%22m21.2%2034.4%2030.4-11.7%2014%2010.6-30.5%2011.8-13.9-10.7Z%22%20fill%3D%22%237EDCFF%22%20stroke%3D%22%230A7D9C%22%20stroke-width%3D%221.2%22%2F%3E%3C%2Fg%3E%3Cpath%20d%3D%22M12%2053h40%22%20stroke%3D%22%2311151B%22%20stroke-opacity%3D%22.18%22%2F%3E%3C%2Fsvg%3E"

FAVICON_LINK = "<link rel='icon' href=\"" + _DARK_URI + "\">"
FAVICON_LINK_LIGHT = "<link rel='icon' href=\"" + _LIGHT_URI + "\">"


def favicon_link(theme: str = "auto") -> str:
    """Icon markup for a resolved theme.

    ``auto`` emits BOTH links, each carrying its own ``media`` query. That is the only way to
    let the icon follow the system while the page is already doing the same; a single icon
    would pin one appearance and be wrong for half the readers. A pinned theme emits exactly
    one, because there a media query would disagree with the page it labels.
    """
    if theme == "light":
        return FAVICON_LINK_LIGHT
    if theme == "dark":
        return FAVICON_LINK
    return (
        "<link rel='icon' media='(prefers-color-scheme: dark)' href=\"" + _DARK_URI + "\">"
        "<link rel='icon' media='(prefers-color-scheme: light)' href=\"" + _LIGHT_URI + "\">"
    )
