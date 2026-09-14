# Vendored webfonts

The three families the AceFleet design system names (`design/tokens/acefleet-design-tokens-dark.css`
in the `ace-fleet` repo): Space Grotesk for display, DM Sans for body, DM Mono for readouts.

**Why they are checked in rather than linked.** acefleet.dev loads these from
`fonts.googleapis.com` (`src/routes/__root.tsx`). The sidecar cannot: it binds loopback only and
its entire claim is that nothing leaves the machine. A stylesheet `<link>` to Google would make
every dashboard load an outbound request that reports, by its timing alone, when a developer was
working. Serving the bytes from `127.0.0.1` keeps the typography and the claim.

**Latin subset only.** The Google CSS ships `latin`, `latin-ext` and `vietnamese` slices per
face; only `latin` is vendored. The dashboard renders English prose, file paths and numbers, and
the other two slices double the payload for glyphs it never emits.

**Two of the four files are variable fonts.** Google serves one file per family for DM Sans and
Space Grotesk and varies the weight axis inside it — the per-weight URLs in its CSS are
byte-identical, which is why `@font-face` here declares a `font-weight` *range* rather than one
value per file. Shipping them per weight cost 212KB for 77KB of distinct bytes.

| File | Family | Weights | Size |
|---|---|---|---:|
| `SpaceGrotesk-var.woff2` | Space Grotesk | 400–700 (variable) | 22 KB |
| `DMSans-var.woff2` | DM Sans | 400–600 (variable) | 37 KB |
| `DMMono-400.woff2` | DM Mono | 400 | 8.7 KB |
| `DMMono-500.woff2` | DM Mono | 500 | 8.7 KB |

## Licence

All three are licensed under the SIL Open Font License 1.1, which permits redistribution
bundled with software. Upstream:

* Space Grotesk — https://github.com/floriankarsten/space-grotesk
* DM Sans, DM Mono — https://github.com/googlefonts/dm-fonts

Retrieved from `fonts.gstatic.com` via the `css2` API. To refresh, re-request the same family
list acefleet.dev uses and keep the `latin` blocks.
