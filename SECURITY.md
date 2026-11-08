# Security

Report vulnerabilities privately to the repository owner rather than
in a public issue.

Design notes:

- This client reads the Muse Gadget SDK's pairing store in process.
  It never prints, logs, or transmits pairing material anywhere except
  the SDK's own authenticated requests to the Muse API and the paired
  VM.
- The OpenAI-compatible shim binds to 127.0.0.1 by default and has no
  built-in authentication. Anyone who can reach the port can send
  messages as the paired device. Do not bind it to a network interface
  without an authenticating, encrypted proxy in front of it.
- Messages sent through this client are attributed to the paired
  gadget device by the server.
