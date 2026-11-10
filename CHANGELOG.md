# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-11-10

First public release, generalised from a handler that watches backups on Scaleway Serverless
Functions.

### Added

- A check of the newest object under a prefix in S3-compatible storage, listed with
  `ListObjectsV2` across every page and signed with AWS Signature Version 4 using only the Python
  standard library. Path-style and virtual-hosted addressing.
- A backup suffix filter (`BACKUP_SUFFIX`, default `.tar.gz.age`), with comma-separated lists and
  `*` for any key, so checksum sidecars and logs never count as backups.
- Six outcomes: `ok`, `stale`, `too-small`, `storage-empty`, `storage-unreadable` and
  `misconfigured`. Every failure to check is an alarm.
- Retries with doubling backoff for transient storage errors, and a page cap (`S3_MAX_PAGES`)
  that reports `storage-unreadable` rather than guessing.
- Alerts by email through Scaleway Transactional Email (one or more recipients), by a JSON
  webhook with an optional bearer token, or both.
- A weekly proof-of-life on a configurable UTC weekday, given by name or number.
- Scaleway CRON trigger support: a `POST` whose JSON body carries `cron_token` equal to
  `CRON_TOKEN` runs as the cron. A cron call is told only the outcome.
- Manual calls with `Authorization: Bearer <SENTINEL_TOKEN>`, and `?notify=1` to send the
  current result.
- Non-HTTP scheduler events (for example on AWS Lambda) run as the cron; Lambda function URL
  requests are recognised as HTTP.
- Refused calls are logged with their method, path and whether a token was present, never the
  token itself.
- A command line: `python3 handler.py`, `--cron` and `--notify`, with exit status 0, 1 or 2.
- Offline tests, including the event a Scaleway CRON trigger really sends and the worked
  Signature Version 4 example from the Amazon S3 documentation.

[Unreleased]: https://github.com/TicketWaveHQ/serverless-backup-sentinel/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/TicketWaveHQ/serverless-backup-sentinel/releases/tag/v1.0.0
