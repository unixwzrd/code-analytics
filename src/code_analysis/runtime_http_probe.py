"""Tool-owned, bounded HTTP readiness probe; never imports subject code."""

from __future__ import annotations

import sys
from email.message import Message
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: Message, newurl: str
    ) -> None:
        return None


def main() -> int:
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            Request(sys.argv[1], method="GET"), timeout=1.0
        ) as response:
            return 0 if response.status == int(sys.argv[2]) else 1
    except (URLError, OSError, ValueError):
        # URLs, credentials and private network details must not leak to diagnostics.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
