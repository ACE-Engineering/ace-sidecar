"""ace.sidecar — the local sidecar: a developer's own proxy in front of Anthropic.

A separate product from the cloud gateway, and now a separate package. It was one module
plus three that lived under ``ace.gateway`` because that is where they were first written,
which made the boundary between the two products invisible:

* ``app`` — the FastAPI app ``ace up`` runs. Mounts ``install_messages_route`` and nothing
  else; every optimization lever is off, because Phase 0 is a measurement release.
* ``insights`` — what the local dashboard reports on a developer's own sessions.
* ``strategies`` — the optimization simulation behind those numbers. Its docstring said
  "importable by the dashboard" while sitting in the gateway; the dashboard is here.
* ``dashboard_render`` — 1,943 lines of HTML. The gateway retired its own pages in the
  2026-07-31 cutover and renders none; this is the only HTML left in the repo, and it belongs
  to the one product that still serves a page.

What it still shares with the gateway is deliberate and small: ``gateway.messages`` /
``messages_auth`` (the seam that exists so this app can be built without ``create_app``'s ~40
parameters) and ``gateway.pricing`` (rate tables, which are a property of the providers, not
of either product). Nothing in ``ace.gateway`` imports anything from here — the dependency
runs one way, and should stay that way.

``build_sidecar_app`` is re-exported so ``from ace.sidecar import build_sidecar_app`` keeps
working: ``ace.cli`` and the tests import it by that name, and the package split is not a
reason to churn their imports.
"""

from ace.sidecar.app import build_sidecar_app

__all__ = ["build_sidecar_app"]
