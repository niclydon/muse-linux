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
  plain-text request cannot reach them without a CORS preflight. The
  web API sends CORS headers only to the bundled Tauri shell.
- Behind a reverse proxy that forwards its own host name (such as
  `tailscale serve`), list that name with `--allow-host` or
  `MUSE_LINUX_ALLOWED_HOSTS` (the web service reads it from
  `/etc/default/muse-linux`; `deploy/install.sh` fills it in from a
  matching `tailscale serve` entry). Anyone who can reach the proxy can
  still act as the paired device, so keep it on a private network.
- Messages sent through this client are attributed to the paired
  gadget device by the server.
