# ADR-0005: Remote conversion over SSH

Status: Accepted, verified (2026-07-15)

## Context

Representative runtime characterization found local Marker conversion
unsuitable for the target laptop. The
owner has a GPU server reachable through an existing SSH alias. Remote
conversion is an optional converter backend, not a distributed scheduler: the
existing job queue still owns global conversion concurrency, paper lanes,
artifact validation, and installation.

## Decision

1. `RemoteConverter` implements the existing converter contract. It copies one
   PDF to a fresh, random remote run directory, invokes the installed
   `paper_pipeline.convert.remote` worker there, downloads a manifest plus the
   canonical `transcription.md`, optional `figures/`, and `pages/`, and returns
   only local staging paths. The converter result's additive `page_paths` field is
   validated through the same local contract as figures.
2. Host alias, absolute POSIX remote root, and remote Python executable come
   only from user-level `AppConfig`. They are never serialized into a library,
   provenance, or stored diagnostics. SSH credentials remain SSH's concern.
3. The default transport uses the system `ssh` and `scp` clients with argv
   execution, strict validation of host/settings, and a deadline inherited from
   `ConversionRequest.timeout_seconds`. No shell text contains local library
   paths.
4. Each attempt uses an unguessable hexadecimal run ID. The remote wrapper
   starts the worker in a new process group, records its PID inside that run
   directory, and traps PTY hangup/termination to kill the group. Timeout and
   ordinary failure also issue a separate, run-ID-scoped cleanup command. The
   cleanup command accepts no caller-provided PID and cannot address a process
   outside the configured run root.
5. Downloaded paths are treated as untrusted: symlinks, unexpected root
   entries, path traversal, missing/empty transcription, and malformed
   manifests fail before anything enters the caller's staging directory.
6. Default tests use a fake transport only. A real-host smoke test is both GPU
   marked and environment-gated, so the default suite never opens a network
   connection or runs Marker.

## Consequences

- Remote setup must install Paper Pipeline with its Marker extra and make the
  configured Python executable available.
- Killing the local conversion child closes the forced PTY; the remote trap is
  the primary crash-safe cancellation path, with pidfile cleanup as the bounded
  ordinary timeout/failure path.
- SSH transport output is not copied into library diagnostics because it may
  contain machine paths or administrator-controlled text. Failures use concise,
  classified messages instead.
- The remote host is a replaceable external edge. Scheduling, durable truth,
  and artifact installation remain local and unchanged.
- The full production path was verified against `noesis` (Ubuntu 24.04.4,
  RTX 3090) using Marker 1.10.2: fresh local child, SSH upload/execution,
  validated download, and cleanup completed successfully.
- Successful conversion logs retain numeric stage timings. A measured 11-page
  run spent 56.0 of 61.8 remote-worker seconds inside Marker conversion;
  imports took 2.7 seconds, model construction 0.7 seconds, and 96-DPI page
  rendering 1.4 seconds. SSH upload and download added about 1.7 seconds.
