# Vendored price & capability feeds

Two MIT-licensed public feeds, snapshotted here so the model catalog stops being purely
hand-entered. Refresh with `python scripts/refresh_price_feeds.py`, then
`python scripts/sync_model_catalog.py` so a built wheel carries the update.

Nothing in the runtime fetches these. `scripts/refresh_price_feeds.py` is the only code in the
repo that opens a socket for them, a refresh lands as a reviewable diff, and
`ace.gateway.llm_router.feeds.FeedSnapshot.load()` reads files.

## What is here

| File | Rows | Source | Licence |
| --- | --: | --- | --- |
| `litellm_prices.jsonl` | 2,261 | [`BerriAI/litellm`](https://github.com/BerriAI/litellm) `model_prices_and_context_window.json` | **MIT** |
| `modelsdev_models.jsonl` | 5,892 | [`anomalyco/models.dev`](https://github.com/anomalyco/models.dev) `api.json` | **MIT** |
| `MANIFEST.json` | — | provenance: URLs, licences, fetch date, upstream sha256, row counts, rejection findings | — |

Records keep **upstream field names verbatim** (`input_cost_per_token`, not `in`). Renaming
happens in code, in `ace.gateway.llm_router.feeds`, under test — so a suspicious price traces
from a routing decision to a line here to a line upstream. One record per line so a refresh
diff is readable: a new model is one added line, a price change is one changed line.

`litellm_prices.jsonl` keeps only rows with `mode` in `{chat, completion, responses}` **and** an
input price. Image, embedding, rerank, moderation and audio modes are priced per image or per
second and do not fit the `per_token` regime; coercing them would produce prices that look real.

## Licences, verified 2026-07-29

**litellm — MIT.** Its `LICENSE` is a split file: everything under `enterprise/` takes
`enterprise/LICENSE`, everything else is MIT. `model_prices_and_context_window.json` is at the
repository root, so it is the MIT half.

**models.dev — MIT.** Verified through the GitHub licence API against the `LICENSE` on the
repository's **default branch, which is `dev`, not `main`** — a raw fetch from `main` 404s and
would read as "no licence" if you stopped there.

## Two feeds evaluated and REJECTED for vendoring

Adapters for both live in `ace.fleetfeeds` and read an operator-supplied local file. No upstream
bytes are committed. `python scripts/refresh_price_feeds.py --fetch-unvendored DIR` will pull
them to a path outside the repo for an operator who has made their own call on the terms.

### SkyPilot catalog — no licence

`skypilot-org/skypilot-catalog` has **no `LICENSE`, `COPYING` or `NOTICE` file anywhere in its
tree** — 257 paths, checked via the GitHub trees API on the default branch — and the GitHub
licence API returns `null`. The `skypilot-org/skypilot` *engine* repo is Apache-2.0, but that is
a separate repository and its licence does not extend here. Unlicensed means all rights reserved
by default, so the CSVs are not redistributable inside an MIT repo.

The leverage map also flagged the refresh cadence as unverified; that half **did** resolve, and
in the feed's favour. The README documents a **7-hour** refresh. It also says only **v7 and v8
are still updated periodically** — so the map's observation that "v5–v8 all return 200" is true
and misleading, because v5 and v6 are frozen files that answer 200 forever. **v8** is the
version to use, and `SKYPILOT_SCHEMA_VERSION` in `ace.fleetfeeds` pins that choice.

### AWS Spot Advisor — no licence stated

`spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json` is unauthenticated and live (34
regions, 1,192 instance types, `{"s": savings_pct, "r": interruption-band index}` per pool), but
AWS publishes no licence with it. It is site content under the AWS Site Terms, which grant no
redistribution right. Same disposition, same reason.

## What these feeds do NOT contain

**Neither feed publishes per-`query_category` quality scores.** models.dev has *support*
booleans — `tool_call`, `reasoning`, `structured_output`, `attachment` — which say what a model
will accept, not how well it answers. So the `capability:` blocks in
`data/model_market/models/*.yaml` and the scores in `CapabilityMatrix` are **not** replaced by
this snapshot, and the leverage map's "the hand-entered `CapabilityMatrix` scores — a data
problem, solved by the feeds above" is too optimistic. The feeds fix the *price and shape* half
of that record (cost, context window, modalities, plus hard gates like "cannot accept an image,
so cannot serve `vision`") and leave the *quality* half exactly where it was.

`python scripts/refresh_price_feeds.py --coverage` prints the field-level version of this.
