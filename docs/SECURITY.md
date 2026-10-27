# Security notes

Aidon can write files, execute generated commands in a sandbox, and connect to external services. Treat it as software with meaningful permissions, not as a harmless chatbot. This page summarizes operational precautions, it is not a formal security audit or a guarantee.

## Security boundaries

- Approval is for decisions, chat is for conversation.
  Questions have no clock and no card, decisions have
  a fail-closed clock.
- Secrets never in chat, never in a trace, never in a
  scan report, never in a key list, only as a hash.
- A plugin scan reports path, line, and type only,
  never values, and never an invented CVE id.
- A trigger starts work, it never approves work.
- A live action is a closed list, never an arbitrary shell.
- A budget exceeded fails a run, it does not continue it.
- A Docker container mounts only the workdir, never
  docker.sock, never host credentials, never a host home.


Other boundaries, from the main guides:

- Nothing goes live without human approval, on a decision card, fail-closed on timeout.
- Model output, research content, and plugin code are untrusted data, never instructions, and are scanned before adoption.
- Keys are never shown on screen, in a video, in logs, or in list responses, only hash prefixes, and a generated v1 key is shown once, then only its hash is stored in a 0600 file.
- Memory records are redacted at record time, credentials redacted, emails not, because recipients are legitimate.
- The full WebUI must never be tunnelled unauthenticated, use a webhook-only listener in front for public reachability.
