# serverless-backup-sentinel

A small, zero-dependency Python function that checks, from outside, that a recent backup really
exists in S3-compatible object storage. It raises an alarm when the newest backup is too old or
suspiciously small, when there is no backup at all, or when the storage cannot be read. Alarms go
by email through Scaleway Transactional Email, to a webhook of your choice, or both. Once a week
it also sends a proof-of-life message, so its silence means something.

**Who it is for:** anyone who backs up to S3-compatible storage (Scaleway Object Storage, AWS S3,
Cloudflare R2, Backblaze B2, MinIO and similar) and wants an independent check that does not run
on the machine that makes the backups. It is built for Scaleway Serverless Functions on a CRON
trigger, and the same file runs from plain cron on any machine with Python 3.10 or later.

It is one file, [`handler.py`](handler.py), using only the Python standard library. There is
nothing to install and nothing to vendor.

## 60-second start

Check your bucket from your own machine first. A plain run only reports; it sends nothing.

```bash
git clone https://github.com/TicketWaveHQ/serverless-backup-sentinel.git
cd serverless-backup-sentinel

export S3_ENDPOINT=https://s3.fr-par.scw.cloud S3_REGION=fr-par
export S3_BUCKET=my-backups-bucket S3_PREFIX=backups/daily/
export BACKUP_SUFFIX=.tar.gz.age MAX_AGE_HOURS=26
read -rs S3_ACCESS_KEY_ID && export S3_ACCESS_KEY_ID           # typed, not echoed
read -rs S3_SECRET_ACCESS_KEY && export S3_SECRET_ACCESS_KEY

python3 handler.py | python3 -m json.tool
```

The exit status is 0 when healthy and 1 on an alarm. The output looks like this (example values):

```json
{
    "event": "backup-sentinel",
    "name": "my-backups-bucket/backups/daily/",
    "checked_at": "2026-11-09T07:30:00Z",
    "bucket": "my-backups-bucket",
    "prefix": "backups/daily/",
    "suffix": ".tar.gz.age",
    "max_age_hours": 26.0,
    "min_size_bytes": 1,
    "objects_seen": 4,
    "matching_objects": 2,
    "key": "backups/daily/db-2026-11-09.tar.gz.age",
    "uploaded": "2026-11-09T01:06:00Z",
    "age_hours": 6.4,
    "size_bytes": 52690944,
    "reason": "ok",
    "alarm": false,
    "detail": "The newest backup is 6.4 hours old; the limit is 26.0 hours.",
    "trigger": "manual",
    "notified": [],
    "notify_errors": {}
}
```

When `reason` is `ok`, [deploy it](#deploy-on-scaleway-serverless-functions), then
[induce a failure](#induce-a-failure). A monitor is only proven once it has been made to fail.

## How it works

Each run:

1. Lists every object under `S3_PREFIX` in `S3_BUCKET` with the S3 `ListObjectsV2` API, following
   continuation tokens page by page. Each request is signed with AWS Signature Version 4 using
   `hashlib` and `hmac` from the standard library.
2. Keeps only keys that end with `BACKUP_SUFFIX` (default `.tar.gz.age`), so checksum sidecars
   (`.sha256`), manifests and logs never count as a backup.
3. Takes the newest of those by `LastModified` and decides:

| Finding | `reason` | Outcome |
|---|---|---|
| The newest matching object is no older than `MAX_AGE_HOURS` and at least `MIN_SIZE_BYTES` | `ok` | Silent, except for the proof-of-life on `PROOF_OF_LIFE_DAY` |
| The newest matching object is older than `MAX_AGE_HOURS` | `stale` | Alarm |
| The newest matching object is smaller than `MIN_SIZE_BYTES` | `too-small` | Alarm |
| No object matches | `storage-empty` | Alarm |
| The storage cannot be listed, the response is not a listing, a timestamp cannot be read, or there are more than `S3_MAX_PAGES` pages | `storage-unreadable` | Alarm |
| The sentinel's own settings are missing or invalid | `misconfigured` | Alarm |

4. Sends the message on every configured channel. It keeps no state: an alarm repeats on every
   scheduled run until the cause is fixed.

Transient storage errors (timeouts, connection failures, HTTP 5xx, 408 and 429) are retried up to
`S3_ATTEMPTS` times with a short backoff, so one network blip does not wake anyone. Permanent
errors such as HTTP 403 alarm at once.

An alarm email reads like this (example values):

```text
Subject: ALARM: backup stale (nightly database)

The newest backup is 30.4 hours old; the limit is 26.0 hours.

  object:   backups/daily/db-2026-11-08.tar.gz.age
  uploaded: 2026-11-08T01:06:00Z
  age:      30.4 hours
  limit:    26.0 hours
  size:     52428800 bytes
  bucket:   my-backups-bucket
  prefix:   backups/daily/
  suffix:   .tar.gz.age
  matching: 1
  listed:   2
  checked:  2026-11-09T07:30:00Z

This check reads the backup storage directly, so it stays accurate when the machine that makes
the backups is the thing that has failed.
This alarm repeats on every scheduled run until the cause is fixed.
```

### The rules it is built on

- **Run somewhere else.** A watchdog on the backup host dies with the host, and its silence reads
  as health. Run the sentinel on a different machine, ideally at a different provider.
- **Check the artefact, not the process.** A script that reports "backup succeeded" only proves
  that a script ran. The sentinel reads the storage itself.
- **"Cannot verify" is an alarm, never health.** If the storage cannot be read, the sentinel says
  so loudly. It never reports a check it could not make as a pass.
- **A channel that only speaks on failure cannot show that it works.** The weekly proof-of-life
  is what tells you the sentinel, its platform and its email path are still alive. **If the
  weekly message stops arriving, treat that as a fault.**

### Who may call it

| Call | How it is recognised | What happens |
|---|---|---|
| Scaleway CRON trigger | An HTTP `POST` whose JSON body carries `cron_token` equal to `CRON_TOKEN` | A scheduled run: alarm on any failure, proof-of-life on its day. The reply holds only the outcome. |
| A platform scheduler event (other platforms) | An event with no HTTP shape at all | The same as a scheduled run |
| Manual call | `Authorization: Bearer <SENTINEL_TOKEN>` | The check runs and the full result comes back as JSON. Nothing is sent unless `?notify=1`, which sends the current result, healthy or not. |
| Anything else | | `401 unauthorised`. The storage is not read and nothing is sent. |

### The failure this design is built around

A scheduled monitor can be broken in a way that no manual test shows: it can refuse its own
scheduler.

A Scaleway CRON trigger calls the function as an HTTP `POST` to `/`, with the trigger's arguments
as the JSON body and no `Authorization` header. A handler that treats every HTTP request as an
outside caller, and demands a bearer token, answers each scheduled run with 401. Manual tests
pass, because they carry the token. Unit tests that model the cron as an event with no HTTP
method pass too, because that is not what Scaleway sends. Nothing alarms, because the thing that
is broken is the alarm.

What this version does about it:

- The trigger's arguments carry `{"cron_token": "..."}`, and a `POST` whose body carries the
  right `cron_token` runs as the cron.
- The tests build the event Scaleway actually sends: a `POST` to `/`, a JSON body (plain or
  base64), no `Authorization` header.
- A refused call is logged with its method, its path and whether it carried a token (never the
  token itself), so a cron that is being refused shows up in the function's logs.
- The [induced failure](#induce-a-failure) goes through the trigger, not a manual call. A manual
  call passes whether or not the cron is accepted.

A related rule shaped the tests. A suite that stubs a client library the handler no longer uses
sends every check of the decision logic down the error path, where failing tests are easy to
miss. The tests here replace only the network connection, and drive the real signing, parsing,
pagination, retry and notification code.

## Configuration

Everything is set through environment variables. Values are trimmed, and an empty value counts as
unset. "Secret" means it should be stored as a secret variable on your platform, never in a
repository.

### Storage

| Variable | Required | Default | Secret | Meaning |
|---|---|---|---|---|
| `S3_ENDPOINT` | yes | | | The storage endpoint, for example `https://s3.fr-par.scw.cloud`. `http://` is accepted for local test servers. |
| `S3_REGION` | | `fr-par` | | The region used in the signature, for example `fr-par`, `eu-west-2`, or `auto` for Cloudflare R2. |
| `S3_BUCKET` | yes | | | The bucket, for example `my-backups-bucket`. |
| `S3_PREFIX` | | empty (whole bucket) | | Only keys under this prefix are listed, for example `backups/daily/`. |
| `S3_ACCESS_KEY_ID` | yes | | yes | An access key that can list the bucket. Nothing more is needed. |
| `S3_SECRET_ACCESS_KEY` | yes | | yes | Its secret key. |
| `S3_ADDRESSING` | | `path` | | `path` sends requests to `endpoint/bucket`; `virtual` sends them to `bucket.endpoint`. |
| `S3_ATTEMPTS` | | `3` | | Attempts per listing page for transient errors. The wait between attempts starts at 1 s and doubles. |
| `S3_MAX_PAGES` | | `50` | | The most pages of 1,000 keys to read. Beyond this the run is `storage-unreadable`: listings come back in key order, not date order, so the newest backup could be on a page that was never read. |

### The check

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `BACKUP_SUFFIX` | | `.tar.gz.age` | What a backup's key ends with. A comma-separated list is accepted (`.dump,.dump.age`). `*` means any key; an empty value means the default. |
| `MAX_AGE_HOURS` | | `26` | Alarm when the newest backup is older than this. Decimals are accepted. |
| `MIN_SIZE_BYTES` | | `1` | Alarm when the newest backup is smaller than this. The default catches zero-byte uploads; `0` turns the size check off. |
| `PROOF_OF_LIFE_DAY` | | `monday` | The UTC weekday of the healthy message: a name (`monday` or `mon`) or a number with **Monday as 0 and Sunday as 6**. Unlike cron, 0 is Monday. |
| `SENTINEL_NAME` | | `bucket/prefix` | A label for subjects and logs, useful when several sentinels share one inbox. |

**Why `.tar.gz.age` is the default suffix.** There is no suffix that suits everyone, so the
default is chosen to fail safe. If your backups are named differently, the first run reports
`storage-empty` and tells you which suffix it looked for. An empty default would do the opposite:
it would count a checksum sidecar or a log file as a backup and report health when the backup
itself was missing. `.tar.gz.age` is the usual name for a tarball encrypted with
[age](https://age-encryption.org); set your own.

**Choosing `MAX_AGE_HOURS`.** The sentinel runs at a fixed time, so a missed backup is caught at
the first run where the newest backup is older than the limit. With a daily backup at 01:00 and a
daily check at 07:30, the newest backup is normally 6.5 hours old at check time. If one night is
missed, it is 30.5 hours old at the next check:

- `MAX_AGE_HOURS=26` catches that missed night at the next check, and still tolerates a backup
  that finishes up to 19.5 hours late.
- `MAX_AGE_HOURS=36` tolerates more lateness, but a single missed night goes unreported until
  the check after next, a day later.

In general, for a backup every *I* hours whose newest copy is normally *d* hours old at check
time, a single missed backup is caught at the next check when `MAX_AGE_HOURS` is below *I* + *d*.
The default of 26 suits a daily backup that finishes at least two hours before the check.

### Alerts by email (Scaleway Transactional Email)

Set all of `TEM_PROJECT_ID`, `TEM_API_KEY`, `ALERT_FROM_EMAIL` and `ALERT_TO_EMAIL`, or none of
them. A half-configured email channel is reported as an error rather than silently skipped.

| Variable | Default | Secret | Meaning |
|---|---|---|---|
| `TEM_PROJECT_ID` | | | The Scaleway project that holds the verified sending domain. |
| `TEM_API_KEY` | | yes | A Scaleway API secret key allowed to send transactional email in that project. |
| `ALERT_FROM_EMAIL` | | | The sender, an address on the verified domain, for example `sentinel@example.com`. |
| `ALERT_TO_EMAIL` | | | One recipient or a comma-separated list, for example `alerts@example.com`. |
| `ALERT_FROM_NAME` | `Backup Sentinel` | | The sender's display name. |
| `TEM_REGION` | `fr-par` | | The region of the Transactional Email API. |

### Alerts by webhook

| Variable | Default | Secret | Meaning |
|---|---|---|---|
| `WEBHOOK_URL` | | yes | An `https://` (or `http://`) URL that receives a JSON `POST`. Treat it as a secret: many webhook URLs embed one. Error messages and logs never include it. |
| `WEBHOOK_TOKEN` | | yes | When set, sent as `Authorization: Bearer <token>`. |

The webhook receives:

```json
{
  "source": "serverless-backup-sentinel",
  "version": "1.0.0",
  "alarm": true,
  "reason": "stale",
  "subject": "ALARM: backup stale (my-backups-bucket/backups/daily/)",
  "text": "ALARM: backup stale (my-backups-bucket/backups/daily/)\n\nThe newest backup is ...",
  "result": { "the full result, as in the 60-second start": "..." }
}
```

The top-level `text` field is the field that Slack and Mattermost incoming webhooks display. Not
every receiver accepts extra fields, so test yours with an [induced failure](#induce-a-failure).
Configure email, the webhook or both. If none is configured, every run that should notify fails
with `notify_errors.config`.

### Access

| Variable | Secret | Meaning |
|---|---|---|
| `SENTINEL_TOKEN` | yes | The bearer token for manual calls. Unset means no manual calls are accepted. |
| `CRON_TOKEN` | no, see [why](#why-cron_token-is-a-plain-variable) | The value the CRON trigger sends as `cron_token`. Unset means no HTTP call can run as the cron. |

Generate each one with `openssl rand -hex 32`, keep them in your password manager, and use a
different value for each.

## Deploy on Scaleway Serverless Functions

### Before you start

1. **A key for the storage.** Create an IAM application with read-only access to Object Storage
   in the bucket's project, and an API key for it. The sentinel only lists keys: it never reads an
   object's contents, and it never writes.
2. **A sending domain.** In Scaleway Transactional Email, add your sending domain and add the SPF
   and DKIM records Scaleway gives you to your DNS. Wait until the domain is verified.
   `ALERT_FROM_EMAIL` must be an address on it.
3. **A key for email.** Create an API key for an IAM application that may send transactional email
   in that project, and note the project ID.
4. **Two tokens.** `openssl rand -hex 32` once for `SENTINEL_TOKEN` and once for `CRON_TOKEN`.

### 1. Package it

```bash
zip function.zip handler.py
```

That is the whole package. There are no dependencies to vendor.

### 2. Create the function

In the Scaleway console, under Serverless Functions:

1. Create a namespace in your chosen region, for example `fr-par`.
2. Create a function in it with:
   - **Runtime:** a Python 3 runtime, 3.10 or later. CI runs the tests on Python 3.10 to 3.14.
   - **Code:** upload `function.zip`.
   - **Handler:** `handler.handle`. The file also exports `handler.handler`, so either works.
   - **Privacy:** public. The handler authorises every call itself (see
     [Who may call it](#who-may-call-it)). If you choose private instead, confirm that the
     trigger still reaches the function by inducing a failure.
   - **Timeout:** a few minutes. When the storage is failing, one listing page can take more than
     a minute (three attempts, each with a 20-second network timeout, plus backoff), and a
     function that times out sends nothing.

To upload a new version later with the Scaleway CLI:

```bash
scw function deploy namespace-id=<namespace id> name=<function name> runtime=python313 \
  zip-file=function.zip region=fr-par
```

### 3. Set the variables

Set these as ordinary environment variables (example values):

```text
S3_ENDPOINT=https://s3.fr-par.scw.cloud
S3_REGION=fr-par
S3_BUCKET=my-backups-bucket
S3_PREFIX=backups/daily/
BACKUP_SUFFIX=.tar.gz.age
MAX_AGE_HOURS=26
PROOF_OF_LIFE_DAY=monday
TEM_PROJECT_ID=<your project id>
ALERT_FROM_EMAIL=sentinel@example.com
ALERT_TO_EMAIL=alerts@example.com
CRON_TOKEN=<the value from openssl>
```

Set these as **secret** environment variables: `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`,
`TEM_API_KEY`, `SENTINEL_TOKEN`, and `WEBHOOK_URL` and `WEBHOOK_TOKEN` if you use a webhook.

### 4. Add the CRON trigger

On the function, add a trigger of type CRON:

- **Schedule:** for example `30 7 * * *`, every day at 07:30. Scaleway CRON triggers run in UTC.
  Schedule it once a day: see [Limits](#limits) on the proof-of-life.
- **Arguments:**

  ```json
  {"cron_token": "<the CRON_TOKEN value>"}
  ```

Scaleway delivers these arguments as the JSON body of a `POST` to `/`. Without them, every
scheduled run is refused with 401, which is the [failure this design is built around](#the-failure-this-design-is-built-around).

### Why CRON_TOKEN is a plain variable

The token's value has to sit in the trigger's arguments, where anyone who can read the function's
configuration can read it. Storing the function's copy as a secret would protect nothing. So it is
set as a plain variable, which keeps it honest about what it is.

That is acceptable because of how little it grants: a run the cron would make anyway. Whoever
holds it can make the sentinel list the bucket and send an alarm (if there is one) or a
proof-of-life (on its day). The reply to a cron call carries only the outcome (`reason`, `alarm`,
which channels were notified); the full result goes to the function's log. The token cannot read
backups, change settings or silence an alarm. The worst misuse is repeated runs, which means
duplicate messages and extra listing requests.

`SENTINEL_TOKEN` is different. It returns the full result and can send messages on demand, so keep
it secret. If either token leaks, generate a new value, and update the variable and (for
`CRON_TOKEN`) the trigger's arguments together.

### 5. First checks

```bash
URL=https://<your function endpoint>
read -rs TOKEN                       # paste SENTINEL_TOKEN; it is not echoed

# Read-only: confirms storage access and reports the newest backup.
curl -s -H "Authorization: Bearer $TOKEN" "$URL" | python3 -m json.tool

# Sends the current result now. When healthy, the "OK: backups healthy" email should arrive.
curl -s -H "Authorization: Bearer $TOKEN" "$URL?notify=1" | python3 -m json.tool
```

These prove the storage key and the email path. They do not prove the cron. That comes next.

## Induce a failure

A control is not working because it was deployed. It is working once it has been made to fail on
purpose and the alarm has arrived. Do this after the first deployment, and again after any change
to the function, its trigger, its keys or the sending domain.

Go through the trigger, not a manual call. A manual call proves the HTTP path and the email path,
but only a run started by the trigger proves that the scheduler reaches the function and is
accepted. That is the part a manual call cannot see.

1. **Pick an empty test prefix**, for example `backups/sentinel-test-empty/`, and confirm it is
   empty from your machine, in the shell from the [60-second start](#60-second-start):

   ```bash
   S3_PREFIX=backups/sentinel-test-empty/ python3 handler.py | python3 -m json.tool
   # expect "reason": "storage-empty"
   ```

2. **Point the function at it.** Set `S3_PREFIX=backups/sentinel-test-empty/` on the function,
   save, and wait until the function is ready again.
3. **Add a one-off CRON trigger** a few minutes ahead, in UTC (`date -u` shows the time). At
   14:05 UTC, for example, use the schedule `10 14 * * *` with the same arguments,
   `{"cron_token": "<the CRON_TOKEN value>"}`.
4. **Wait for it to fire, and confirm the alarm arrives:** an email with the subject
   `ALARM: no backup found (...)`, and the webhook message if you use one. In the function's logs,
   expect a line with `"trigger": "cron"`, `"reason": "storage-empty"` and `"notified": ["email"]`.
5. **Put everything back.** Restore `S3_PREFIX`, delete the one-off trigger (otherwise it fires
   every day at that time), and confirm with the read-only call from step 5 above that `reason` is
   `ok` again.
6. **Write down the date** the induced failure passed.

If no alarm arrives, the logs say where it stopped:

| In the logs | Meaning |
|---|---|
| `"refused": true` with `"body_has_cron_token": false` | The trigger's arguments do not carry `cron_token`. |
| `"refused": true` with `"body_has_cron_token": true` | The trigger's `cron_token` does not match the function's `CRON_TOKEN`. |
| `"notify_errors": {"email": ...}` | Transactional Email refused the message, for example because the sender's domain is not verified. |
| `"notify_errors": {"config": ...}` | No alert channel is configured. |
| No line at all at that time | The trigger did not fire: check its schedule, and remember it runs in UTC. |

Other failures worth inducing the same way: `MAX_AGE_HOURS=0` gives `stale`, and an invalid
`S3_BUCKET` gives `storage-unreadable`.

## Running it elsewhere

The handler is plain Python. It takes an event dictionary and returns
`{"statusCode", "headers", "body"}`. Email through Transactional Email is an HTTPS API call, so it
works from anywhere.

**Plain cron or a systemd timer.** On any machine other than the one that makes the backups:

```bash
python3 handler.py --cron      # the scheduled run: alarm on failure, proof-of-life on its day
python3 handler.py --notify    # send the current result now, healthy or not
python3 handler.py             # report only
```

The exit status is 0 when healthy, 1 on an alarm and 2 when an alert could not be sent, so a
systemd unit or a CI job fails visibly as well. Keep the variables in a file that only the
service's user can read, for example:

```text
30 7 * * * set -a; . /etc/backup-sentinel.env; set +a; python3 /opt/backup-sentinel/handler.py --cron
```

**AWS Lambda.** Use a Python runtime with the handler `handler.handler`, and schedule it with an
EventBridge schedule whose input is an empty JSON object. A scheduled event has no HTTP shape, so
it runs as the cron, and only principals allowed to invoke the function can send one. `CRON_TOKEN`
is not needed there. If you add a function URL, its requests are recognised as HTTP (by
`requestContext.http`) and need the bearer token like any other manual call.

**Other function platforms.** If the platform's scheduler sends an HTTP request, as Scaleway's
does, put `{"cron_token": "..."}` in its body. If it sends a non-HTTP event, the event runs as the
cron. Either way, induce a failure through the scheduler before you rely on it.

**GitHub Actions.** A scheduled workflow can run `python3 handler.py --cron` with the variables
stored as repository secrets. Scheduled runs can be delayed, and in a public repository GitHub
disables scheduled workflows after 60 days without repository activity. The weekly proof-of-life
is how you find out.

## Limits

- **Existence is not integrity.** The sentinel proves that an object with the right name exists,
  is recent, and is not smaller than `MIN_SIZE_BYTES`. It does not download, decrypt, checksum or
  restore anything. Only a restore test proves a backup can be restored: schedule one.
- **It trusts the storage's upload time.** `LastModified` is when the object was written, so a
  fresh copy of an old backup looks new.
- **One bucket and prefix per deployment.** Deploy one sentinel per backup stream, and give each
  a `SENTINEL_NAME`.
- **Every run lists every key under the prefix.** With long retention, use a narrower prefix (by
  year or month, for example) or raise `S3_MAX_PAGES`. Past the cap, the run is reported as
  `storage-unreadable` rather than guessed.
- **When it cannot run, it cannot alarm.** If the function's platform is down, or the function
  times out, nothing is sent. The weekly proof-of-life is the only signal for that, so notice when
  it is missing.
- **The proof-of-life is sent on every scheduled run on its day.** Schedule the trigger once a
  day, or expect one healthy message per run that day.
- **No snoozing, deduplication or escalation.** An alarm repeats on every scheduled run until the
  cause is fixed.
- **Tested offline.** The tests use a fake network. The signer matches the worked example in the
  Amazon S3 Signature Version 4 documentation, and the handler this project grew from runs on
  Scaleway Serverless Functions with Transactional Email. Other storage providers are expected to
  work because they implement the same API, but this repository does not test against them.

## Security

- Give the storage key read-only access, limited to the one bucket where your provider allows it
  (on some providers that takes a bucket policy), and give the email key the right to send email
  and nothing more.
- A call that is neither the cron nor carries `SENTINEL_TOKEN` gets 401 before any storage
  request is made.
- Tokens are compared in constant time. Keys and tokens never appear in replies or logs, and
  webhook errors never include the webhook URL.
- To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests need no install, no credentials and no network. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [CHANGELOG.md](CHANGELOG.md).

## Licence

MIT. See [LICENSE](LICENSE).

Maintained by [TicketWave HQ](https://ticketwavehq.com), a UK company (Companies House 17143167).
