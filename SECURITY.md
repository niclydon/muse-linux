# Security

Report vulnerabilities privately to the repository owner rather than
in a public issue.

Design notes:

- This client reads the Muse Gadget SDK's pairing store in process.
  It never prints, logs, or transmits pairing material anywhere except
  the SDK's own authenticated requests to the Muse API and the paired
  VM.
- The OpenAI-compatible shim and the web API (`muse-linux serve`) bind
  to 127.0.0.1 by default and have no built-in authentication. Any
  local program that can reach the port can send messages as the
  paired device. Do not bind them to a network interface without an
  authenticating, encrypted proxy in front of them.
- Web pages cannot use them, even though a browser can reach
  127.0.0.1: both refuse a `Host` header that is not loopback or
  explicitly allowed (against DNS rebinding), refuse requests whose
  `Origin` is a page on another host, and require a JSON (or, for
  voice notes, WAV) content type on writes, so a cross-site form or
  plain-text request cannot reach them without a CORS preflight. A
  browser request with no `Origin` that says it came from another site
  (an image tag or a no-cors fetch) is refused as well, unless it is
  the user following a link. The web API sends CORS headers only to
  Tauri app origins (`tauri://localhost`), which every Tauri app on
  the machine shares, not only the bundled shell.
- Behind any reverse proxy, list the name people use to open it with
  `--allow-host` or `MUSE_LINUX_ALLOWED_HOSTS` (comma-separated; the
  web service reads it from `/etc/default/muse-linux`). That holds even
  for a proxy that rewrites `Host` to localhost, because the browser's
  `Origin` still carries the public name. `deploy/install.sh` fills it
  in from a matching `tailscale serve` entry and skips any name on
  Tailscale Funnel, which is the public internet. Anyone who can reach
  the proxy can still act as the paired device, so keep it on a
  private network.
- Other callers that address the server by a name or address that is
  not loopback need the same allowance: the shim used from a Docker
  container (`--allow-host host.docker.internal` or the bridge address
  such as `172.17.0.1`), or a server bound to `0.0.0.0` and reached by
  the machine's own name. Binding to a specific address allows that
  address automatically.
- Messages sent through this client are attributed to the paired
  gadget device by the server.
