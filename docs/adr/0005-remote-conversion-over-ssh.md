# ADR-0005: Remote conversion over SSH

Status: Accepted

## Context

Marker conversion may need a GPU unavailable on the machine that owns the
library. Remote execution must not introduce a second scheduler or move
library ownership to another host.

## Decision

`RemoteConverter` implements the existing converter contract over the system
`ssh` and `scp` clients.

For each attempt it:

1. creates a random directory below a configured remote root (e.g. `/tmp/`);
2. uploads one source PDF;
3. runs the Paper Pipeline conversion worker with a configured Python
   executable;
4. downloads the manifest and conversion artifacts into local staging;
5. validates them through the normal converter contract; and
6. removes the remote attempt directory.

The local job system continues to own scheduling, paper lanes, cancellation,
validation, and atomic artifact installation. Host, remote root, and Python
executable are user-level settings.

Downloaded content is untrusted. Symlinks, unexpected files, path traversal,
malformed manifests, and missing required artifacts fail validation. Remote
processes run in their own process group so cancellation and timeouts can
terminate the complete attempt.

Default tests use a fake transport. Real-host tests require explicit opt-in.

## Consequences

The remote host must have the same Paper Pipeline code and Marker dependencies
available. SSH authentication remains the user's responsibility. Transport
output and remote paths are excluded from library artifacts and provenance.
