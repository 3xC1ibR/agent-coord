"""HTTP serving for local endpoints without a reverse-DNS startup dependency."""

from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer resolves the bound address only for its cosmetic server_name.
        # Local control endpoints must remain available when DNS is unavailable.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)
