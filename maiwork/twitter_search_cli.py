"""Run the managed twitter-cli with authenticated bootstrap and live search ID.

Executed by the platform venv, so optional dependencies stay outside MaiBot.
Credentials remain in the environment and are scoped to x.com in the cookie jar.
"""
from __future__ import annotations

import os


class SearchOperationUnavailable(RuntimeError):
    """The authenticated bundle scan did not discover SearchTimeline."""


def prepare_search(client_module, graphql_module) -> None:
    session = client_module._get_cffi_session()
    for name, variable in (("auth_token", "TWITTER_AUTH_TOKEN"), ("ct0", "TWITTER_CT0")):
        session.cookies.set(name, os.environ[variable], domain="x.com", path="/")
    # Scan while authenticated, before the CLI chooses its stale fallback ID.
    # The upstream scanner fetches only X's referenced JS bundles; the cookie jar
    # never forwards the login cookies to their abs.twimg.com host.
    graphql_module._scan_bundles(client_module._url_fetch)
    if "SearchTimeline" not in graphql_module._cached_query_ids:
        raise SearchOperationUnavailable("live search operation unavailable")


def main() -> None:
    import json
    import logging

    logging.disable(logging.CRITICAL)
    stage = "import"
    try:
        from twitter_cli import client, graphql
        from twitter_cli.cli import cli

        stage = "scan"
        prepare_search(client, graphql)
    except Exception as exc:
        # Never expose upstream diagnostics, response bodies or credentials.
        if isinstance(exc, SearchOperationUnavailable):
            stage = "no_search_op"
        print(json.dumps({"ok": False, "error": {"code": "bootstrap_failed", "stage": stage}}))
        raise SystemExit(1) from None
    cli()


if __name__ == "__main__":
    main()
