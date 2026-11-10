# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes |

## Reporting a vulnerability

Email **hello@ticketwavehq.com**. Please do not open a public issue. The company's disclosure
policy is at [ticketwavehq.com/security](https://ticketwavehq.com/security).

Include the version or commit, what you found, how to reproduce it, and what an attacker could do
with it. Do not send real credentials or real backup data: example values are enough.

We will reply to confirm that we have your report, and keep you informed while we work on a fix.
Please allow a reasonable time for a fix to be released before you disclose the problem publicly.

## Scope

In scope:

- Bypassing the authorisation rules: running the check, reading a result or sending messages
  without `CRON_TOKEN` or `SENTINEL_TOKEN`.
- Keys, tokens or webhook URLs leaking into replies, logs or error messages.
- Flaws in the Signature Version 4 signing.
- Anything that makes a failed or impossible check report as healthy.

Out of scope:

- Vulnerabilities in Scaleway, other cloud platforms or storage providers. Please report those to
  the provider.
- Weak or exposed configuration in a particular deployment.
- The documented property of `CRON_TOKEN`: anyone who can read the CRON trigger's arguments can
  read it, and can then cause the runs the cron would make anyway. See "Why CRON_TOKEN is a plain
  variable" in [README.md](README.md).
