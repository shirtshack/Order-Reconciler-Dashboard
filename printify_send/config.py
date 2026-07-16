"""Credential loading — the single place this project reads secrets from.

Credentials live in dev/api-credentials/secrets.toml (the cross-project source of
truth). Reading them directly means a brand added there appears in the dashboard
and the CLI with no further work.

This project used to keep its own copy at .streamlit/secrets.toml. That copy went
stale silently: two stores were fully credentialed centrally but absent here, so
they never showed on the dashboard and nothing flagged it. The copy is retained
only as a fallback (see load_secrets).

Note for future edits: this repo is PUBLIC. Do not name stores here — the store
list is discovered from the central file at runtime and never needs hardcoding.
"""
import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # stdlib only on 3.11+
    # Streamlit Community Cloud pins no Python version for this app (no runtime.txt)
    # and has bumped its image before. Cloud never needs tomllib — the central path
    # doesn't exist there, so it takes the st.secrets fallback — so degrade quietly
    # rather than crash the hosted dashboard on import.
    tomllib = None

# Derived from the home dir rather than hardcoded: keeps a username out of this
# public repo, and resolves on whichever machine has dev/ synced via Dropbox.
# Override with RECONCILER_SECRETS if the Dropbox root ever moves.
CENTRAL_SECRETS = Path(
    os.environ.get("RECONCILER_SECRETS")
    or Path.home() / "Dropbox" / "dev" / "api-credentials" / "secrets.toml"
)
LOCAL_FALLBACK = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"

# Feather Found is reconciled by Kennedy in featherfound-reconciler/, with its
# credentials deliberately isolated. It has a [printify.*] section in the central
# file, so without an explicit exclusion it would surface in this dashboard.
EXCLUDED_STORES = {"featherfound"}


def load_secrets(fallback=None) -> dict:
    """Return the raw secrets mapping.

    Prefers the central file. `fallback` is for Streamlit Community Cloud, where
    the central path doesn't exist and secrets are pasted via the Cloud UI —
    callers pass st.secrets. Off Streamlit, we fall back to the local copy.
    """
    if tomllib is not None and CENTRAL_SECRETS.exists():
        with CENTRAL_SECRETS.open("rb") as fh:
            return tomllib.load(fh)
    if fallback is not None:
        return fallback
    if tomllib is not None and LOCAL_FALLBACK.exists():
        with LOCAL_FALLBACK.open("rb") as fh:
            return tomllib.load(fh)
    raise FileNotFoundError(
        f"No credentials found. Expected {CENTRAL_SECRETS} (is Dropbox synced?) "
        f"or {LOCAL_FALLBACK}."
        + ("" if tomllib is not None else " NOTE: Python < 3.11, so no tomllib — "
           "only the st.secrets fallback is available here.")
    )


def load_cfg(fallback=None) -> dict:
    """Shopify + Printify sections, normalised to plain dicts.

    dict(v) flattens both tomllib tables and Streamlit AttrDicts so downstream
    `cfg["shopify"][key]` indexing behaves identically whichever source was used.
    """
    raw = load_secrets(fallback)
    return {
        "shopify": {k: dict(v) for k, v in raw.get("shopify", {}).items()},
        "printify": {k: dict(v) for k, v in raw.get("printify", {}).items()},
    }


def all_stores(cfg: dict) -> list[str]:
    """Store keys usable by the reconciler: paired on both APIs, not excluded."""
    paired = set(cfg.get("shopify", {})) & set(cfg.get("printify", {}))
    return sorted(paired - EXCLUDED_STORES)
