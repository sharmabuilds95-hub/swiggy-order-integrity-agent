"""Local loopback HTTP listener for the Swiggy OAuth 2.1 + PKCE redirect.

`mcp.client.auth.OAuthClientProvider` drives the actual PKCE + Dynamic
Client Registration flow; it just needs two callbacks: somewhere real to
send the user's browser, and a real way to catch the authorization code
Swiggy sends back to SWIGGY_REDIRECT_URI (http://localhost:8765/callback
by default). This module is that loopback catcher.
"""

from __future__ import annotations

import functools
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import anyio

from mcp.client.auth import AuthorizationCodeResult
from mcp.client.auth.exceptions import OAuthFlowError

# How long to wait for the user to finish login in the browser before giving
# up. Without this the listener thread is joined with no deadline and hangs
# forever if the login is never completed (backlog §B; bites at Day-5 demo
# recording). Five minutes is generous for a phone+OTP round-trip.
DEFAULT_CALLBACK_TIMEOUT = 300.0


class LoopbackOAuthFlow:
    """One-shot local HTTP server that catches a single OAuth redirect.

    Serves until a request carrying `code` or `error` arrives (ignoring
    incidental requests like a browser's favicon fetch), then shuts itself
    down. `HTTPServer.shutdown()` must be called from a thread other than
    the one running `serve_forever()` — done here from inside the handler.
    """

    def __init__(
        self,
        host: str,
        port: int,
        callback_path: str,
        timeout: float = DEFAULT_CALLBACK_TIMEOUT,
    ) -> None:
        self._host = host
        self._port = port
        self._callback_path = callback_path
        self._timeout = timeout
        self._params: dict[str, str] | None = None
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    async def redirect(self, authorization_url: str) -> None:
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = parse_qs(urlparse(self.path).query)
                params = {k: v[0] for k, v in query.items()}
                is_callback = "code" in params or "error" in params

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                if is_callback:
                    self.wfile.write(
                        b"<html><body>Swiggy login complete \xe2\x80\x94 you can close this tab.</body></html>"
                    )
                    flow._params = params
                    threading.Thread(target=flow._server.shutdown, daemon=True).start()  # type: ignore[union-attr]

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
                pass

        try:
            self._server = HTTPServer((self._host, self._port), Handler)
        except OSError as exc:
            raise OAuthFlowError(
                f"Could not bind the OAuth callback listener on {self._host}:{self._port} ({exc}). "
                f"Something else is using that port — free it, or change SWIGGY_REDIRECT_URI in .env "
                f"to a different port (and keep the redirect URI's port in sync with it)."
            ) from exc

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        webbrowser.open(authorization_url)

    async def wait_for_callback(self) -> AuthorizationCodeResult:
        if self._thread is None:
            raise OAuthFlowError("redirect() must be called before wait_for_callback()")
        # Bounded join: thread.join(timeout) returns whether or not the callback
        # arrived, so we check is_alive() afterwards. A plain anyio cancel can't
        # interrupt a blocking sync join, hence the timeout is pushed into join
        # itself. On expiry, shut the server down so its thread can exit and the
        # port is freed rather than leaked.
        await anyio.to_thread.run_sync(functools.partial(self._thread.join, self._timeout))
        if self._thread.is_alive():
            if self._server is not None:
                threading.Thread(target=self._server.shutdown, daemon=True).start()
            raise OAuthFlowError(
                f"Timed out after {self._timeout:.0f}s waiting for the Swiggy login callback "
                f"— no login was completed. Re-run and finish the browser login, or set a "
                f"longer timeout."
            )

        params = self._params or {}
        if "error" in params:
            raise OAuthFlowError(f"Swiggy authorization failed: {params.get('error_description', params['error'])}")
        if "code" not in params:
            raise OAuthFlowError(f"Callback finished without a 'code' parameter: {params}")
        return AuthorizationCodeResult(code=params["code"], state=params.get("state"), iss=params.get("iss"))
