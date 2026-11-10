#!/usr/bin/env python3
"""serverless-backup-sentinel: an outside check that a recent backup exists.

A zero-dependency Python handler for Scaleway Serverless Functions, which also
runs on other function platforms and from plain cron. Each run it:

  1. lists an S3-compatible bucket under a prefix (ListObjectsV2, every page,
     signed with AWS Signature Version 4 using only the standard library);
  2. picks the newest object whose key ends with the configured suffix;
  3. raises an alarm when that object is older than MAX_AGE_HOURS or smaller
     than MIN_SIZE_BYTES, when no such object exists, when the storage cannot
     be read, or when the sentinel's own configuration is invalid;
  4. on the configured weekday, sends a proof-of-life message when all is well.

Three rules shape the code:

  * It runs somewhere other than the thing it watches. A watchdog on the
    backup host dies with the host, and its silence reads as health.
  * It checks the artefact, not the process. A script reporting success only
    proves that a script ran.
  * "Cannot verify" is an alarm, never health. evaluate() never raises: every
    failure to check becomes a result with alarm set to True.

Configuration is through environment variables only. See README.md.
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

__version__ = "1.0.0"

DEFAULT_SUFFIX = ".tar.gz.age"
DEFAULT_MAX_AGE_HOURS = "26"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
TEM_URL = "https://api.scaleway.com/transactional-email/v1alpha1/regions/{region}/emails"
HTTP_TIMEOUT_SECONDS = 20
USER_AGENT = "serverless-backup-sentinel/%s" % __version__
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Indirections the tests replace, so that they drive the real signing, parsing
# and notification code against a fake network instead of stubbing it out.
_urlopen = urllib.request.urlopen
_sleep = time.sleep


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ConfigError(Exception):
    """The sentinel's own configuration is missing or invalid."""


class StorageError(Exception):
    """The storage could not be listed. status is the HTTP status, if there was one."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NotifyError(Exception):
    """An alert channel refused or failed to take a message."""


def _describe(exc):
    if isinstance(exc, (ConfigError, StorageError, NotifyError)):
        return str(exc)
    return "%s: %s" % (type(exc).__name__, exc)


# ------------------------------------------------------------ configuration

def _get(env, name, default=None):
    """An environment value with surrounding whitespace removed. Empty counts as unset."""
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _number(env, name, default, kind):
    raw = _get(env, name, default)
    try:
        value = kind(raw)
    except (TypeError, ValueError):
        raise ConfigError("%s must be a number, got %r" % (name, raw))
    if not math.isfinite(value) or value < 0:
        raise ConfigError("%s must be a finite number of at least 0, got %r" % (name, raw))
    return value


def parse_weekday(raw):
    """PROOF_OF_LIFE_DAY: a day name ("monday" or "mon") or Python's weekday(), Monday=0 to Sunday=6."""
    value = (raw or "").strip().lower()
    if value.isdigit() and int(value) <= 6:
        return int(value)
    for index, day in enumerate(DAYS):
        if value in (day, day[:3]):
            return index
    raise ConfigError("PROOF_OF_LIFE_DAY must be a day name (monday or mon) or 0 to 6 with Monday=0, got %r" % raw)


def _suffixes(env):
    """BACKUP_SUFFIX as a tuple for str.endswith. Unset or empty gives the default; "*" any key."""
    items = [item.strip() for item in _get(env, "BACKUP_SUFFIX", "").split(",") if item.strip()]
    if not items:
        return (DEFAULT_SUFFIX,)
    if items == ["*"]:
        return ("",)  # every key ends with ""
    return tuple(items)


def load_config(env):
    """Everything the check needs, validated. Raises ConfigError."""
    names = ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
    missing = [name for name in names if not _get(env, name)]
    if missing:
        raise ConfigError("missing required environment variable(s): %s" % ", ".join(missing))

    endpoint = _get(env, "S3_ENDPOINT")
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ConfigError("S3_ENDPOINT must be a URL such as https://s3.fr-par.scw.cloud, got %r" % endpoint)

    addressing = _get(env, "S3_ADDRESSING", "path").lower()
    if addressing not in ("path", "virtual"):
        raise ConfigError("S3_ADDRESSING must be path or virtual, got %r" % addressing)

    attempts = _number(env, "S3_ATTEMPTS", "3", int)
    max_pages = _number(env, "S3_MAX_PAGES", "50", int)
    if attempts < 1 or max_pages < 1:
        raise ConfigError("S3_ATTEMPTS and S3_MAX_PAGES must be at least 1")

    bucket = _get(env, "S3_BUCKET")
    prefix = _get(env, "S3_PREFIX", "")
    parse_weekday(_get(env, "PROOF_OF_LIFE_DAY", "monday"))  # a typo here is caught every run
    return {
        "endpoint": endpoint,
        "region": _get(env, "S3_REGION", "fr-par"),
        "bucket": bucket,
        "prefix": prefix,
        "access_key": _get(env, "S3_ACCESS_KEY_ID"),
        "secret_key": _get(env, "S3_SECRET_ACCESS_KEY"),
        "addressing": addressing,
        "attempts": attempts,
        "max_pages": max_pages,
        "suffixes": _suffixes(env),
        "max_age_hours": _number(env, "MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS, float),
        "min_size_bytes": _number(env, "MIN_SIZE_BYTES", "1", int),
        "label": _get(env, "SENTINEL_NAME", "%s/%s" % (bucket, prefix)),
    }


# ------------------------------------------------------------ SigV4 listing

def _quote(value):
    return urllib.parse.quote(value, safe="-_.~")


def canonical_query(params):
    return "&".join("%s=%s" % (_quote(k), _quote(v)) for k, v in sorted(params.items()))


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key, datestamp, region, service="s3"):
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def sign_request(method, host, path, params, access_key, secret_key, region, now):
    """AWS Signature Version 4 for a request with an empty body. Returns the headers to send."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = amz_date[:8]
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([
        method, path, canonical_query(params),
        "host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n" % (host, EMPTY_SHA256, amz_date),
        signed_headers, EMPTY_SHA256])
    scope = "%s/%s/s3/aws4_request" % (datestamp, region)
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, datestamp, region),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": EMPTY_SHA256,
        "Authorization": "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                         % (access_key, scope, signed_headers, signature),
    }


def build_list_request(cfg, continuation_token=None, now=None):
    """One signed ListObjectsV2 request, path-style (endpoint/bucket) or virtual-hosted (bucket.endpoint)."""
    parts = urllib.parse.urlsplit(cfg["endpoint"])
    base_path = parts.path.rstrip("/")
    if cfg["addressing"] == "virtual":
        host = "%s.%s" % (cfg["bucket"], parts.netloc)
        path = base_path + "/"
    else:
        host = parts.netloc
        path = "%s/%s" % (base_path, _quote(cfg["bucket"]))
    params = {"list-type": "2", "prefix": cfg["prefix"], "max-keys": "1000"}
    if continuation_token:
        params["continuation-token"] = continuation_token
    headers = sign_request("GET", host, path, params, cfg["access_key"], cfg["secret_key"],
                           cfg["region"], now or _utcnow())
    headers["User-Agent"] = USER_AGENT
    url = "%s://%s%s?%s" % (parts.scheme, host, path, canonical_query(params))
    return urllib.request.Request(url, headers=headers, method="GET")


def _local(tag):
    """An XML tag without its namespace, so responses with or without xmlns both parse."""
    return tag.rsplit("}", 1)[-1]


def _error_body(exc, limit=4096):
    """The body of an HTTPError, read once; the error is closed so its connection is released."""
    try:
        return exc.read(limit) or b""
    except Exception:
        return b""
    finally:
        try:
            exc.close()
        except Exception:
            pass


def _s3_error_code(body):
    try:
        root = ET.fromstring(body)
        code = next((c.text for c in root if _local(c.tag) == "Code"), None)
        return " (%s)" % code if code else ""
    except Exception:
        return ""


def parse_list_response(body):
    """A ListObjectsV2 response as ([{key, last_modified, size}], next continuation token or None)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise StorageError("the listing response is not XML: %s" % exc)
    if _local(root.tag) != "ListBucketResult":
        raise StorageError("unexpected listing response <%s>" % _local(root.tag))
    objects, truncated, token = [], False, None
    for child in root:
        name = _local(child.tag)
        if name == "Contents":
            fields = {_local(c.tag): (c.text or "") for c in child}
            size = fields.get("Size", "").strip()
            objects.append({
                "key": fields.get("Key") or None,
                "last_modified": fields.get("LastModified") or None,
                "size": int(size) if size.isdigit() else None,
            })
        elif name == "IsTruncated":
            truncated = (child.text or "").strip().lower() == "true"
        elif name == "NextContinuationToken":
            token = (child.text or "").strip() or None
    if truncated and not token:
        raise StorageError("the listing is truncated but carries no continuation token")
    return objects, token


def list_page(cfg, continuation_token=None):
    request = build_list_request(cfg, continuation_token)
    try:
        with _urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise StorageError("HTTP %s%s" % (exc.code, _s3_error_code(_error_body(exc))), status=exc.code)
    return parse_list_response(body)


def _retryable(exc):
    if isinstance(exc, StorageError) and exc.status is not None:
        return exc.status >= 500 or exc.status in (408, 429)
    return True  # timeouts, resets, DNS failures, a truncated body


def _with_retries(call, attempts):
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            if attempt == attempts or not _retryable(exc):
                raise
        _sleep(2 ** (attempt - 1))


def list_objects(cfg):
    """Every object under the prefix, following continuation tokens up to S3_MAX_PAGES pages."""
    objects, token = [], None
    for _ in range(cfg["max_pages"]):
        page, token = _with_retries(lambda t=token: list_page(cfg, t), cfg["attempts"])
        objects.extend(page)
        if not token:
            return objects
    # ListObjectsV2 returns keys in name order, not time order, so the newest
    # backup may be on a page that was never read: that is "cannot verify".
    raise StorageError("more than %d pages of objects under the prefix; narrow S3_PREFIX "
                       "or raise S3_MAX_PAGES" % cfg["max_pages"])


# ------------------------------------------------------------ the check

_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def parse_timestamp(value):
    """An S3 LastModified value (2026-11-10T09:30:00.000Z and close variants) as an aware UTC datetime."""
    match = _TIMESTAMP.match((value or "").strip())
    if not match:
        raise ValueError("unrecognised timestamp %r" % value)
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    dt = datetime.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                           tzinfo=datetime.timezone.utc)
    if fraction:
        dt += datetime.timedelta(microseconds=int((fraction + "000000")[:6]))
    if zone and zone != "Z":
        offset = datetime.timedelta(hours=int(zone[1:3]), minutes=int(zone[-2:]))
        dt = dt - offset if zone[0] == "+" else dt + offset
    return dt


def _finish(result, reason, detail):
    result.update(reason=reason, alarm=reason != "ok", detail=detail)
    return result


def evaluate(env=None, now=None):
    """Check the storage once. Never raises: failing to check is itself an alarm."""
    env = os.environ if env is None else env
    now = now or _utcnow()
    result = {"name": _get(env, "SENTINEL_NAME"), "checked_at": _iso(now)}
    try:
        cfg = load_config(env)
    except ConfigError as exc:
        return _finish(result, "misconfigured", "The sentinel's configuration is invalid: %s." % exc)

    where = "s3://%s/%s" % (cfg["bucket"], cfg["prefix"])
    wanted = " or ".join(s for s in cfg["suffixes"] if s) or "anything"
    result.update(name=cfg["label"], bucket=cfg["bucket"], prefix=cfg["prefix"],
                  suffix=",".join(cfg["suffixes"]), max_age_hours=cfg["max_age_hours"],
                  min_size_bytes=cfg["min_size_bytes"])
    try:
        objects = list_objects(cfg)
    except Exception as exc:
        return _finish(result, "storage-unreadable", "Could not list %s: %s." % (where, _describe(exc)))

    candidates = [o for o in objects
                  if o["key"] and not o["key"].endswith("/") and o["key"].endswith(cfg["suffixes"])]
    result.update(objects_seen=len(objects), matching_objects=len(candidates))
    if not candidates:
        return _finish(result, "storage-empty",
                       "No object under %s has a key ending in %s." % (where, wanted))

    try:
        dated = [(parse_timestamp(o["last_modified"]), o) for o in candidates]
    except ValueError as exc:
        return _finish(result, "storage-unreadable",
                       "The listing of %s carries a timestamp that cannot be read: %s." % (where, exc))
    uploaded, newest = max(dated, key=lambda pair: pair[0])
    age = (now - uploaded).total_seconds() / 3600.0
    result.update(key=newest["key"], uploaded=_iso(uploaded), age_hours=round(age, 1),
                  size_bytes=newest["size"])

    if age > cfg["max_age_hours"]:
        return _finish(result, "stale", "The newest backup is %.1f hours old; the limit is %s hours."
                       % (age, cfg["max_age_hours"]))
    if cfg["min_size_bytes"] > 0:
        if newest["size"] is None:
            return _finish(result, "storage-unreadable",
                           "The listing does not report a size for %s." % newest["key"])
        if newest["size"] < cfg["min_size_bytes"]:
            return _finish(result, "too-small", "The newest backup is %d bytes; the minimum is %d bytes."
                           % (newest["size"], cfg["min_size_bytes"]))
    return _finish(result, "ok", "The newest backup is %.1f hours old; the limit is %s hours."
                   % (age, cfg["max_age_hours"]))


# ------------------------------------------------------------ messages

SUBJECTS = {
    "ok": "OK: backups healthy",
    "stale": "ALARM: backup stale",
    "too-small": "ALARM: newest backup too small",
    "storage-empty": "ALARM: no backup found",
    "storage-unreadable": "ALARM: cannot verify backups",
    "misconfigured": "ALARM: backup sentinel misconfigured",
}

CLOSING = {
    "stale": "This check reads the backup storage directly, so it stays accurate when the machine "
             "that makes the backups is the thing that has failed.",
    "too-small": "An object this small is unlikely to be a complete backup.",
    "storage-empty": "No backup exists under this prefix.",
    "storage-unreadable": "This is treated as a failure. A backup that cannot be verified is not a "
                          "working backup.",
    "misconfigured": "Until the configuration is fixed, nothing is being checked.",
}

FIELDS = (
    ("object", "key", "%s"), ("uploaded", "uploaded", "%s"), ("age", "age_hours", "%s hours"),
    ("limit", "max_age_hours", "%s hours"), ("size", "size_bytes", "%s bytes"),
    ("bucket", "bucket", "%s"), ("prefix", "prefix", "%s"), ("suffix", "suffix", "%s"),
    ("matching", "matching_objects", "%s"), ("listed", "objects_seen", "%s"),
    ("checked", "checked_at", "%s"),
)


def render(result):
    """The (subject, plain-text body) of the message for one result."""
    subject = "%s (%s)" % (SUBJECTS.get(result["reason"], "ALARM: backup check failed"),
                           result.get("name") or "backups")
    lines = [subject, "", result["detail"], ""]
    for label, field, fmt in FIELDS:
        if result.get(field) not in (None, ""):
            lines.append("  %-9s %s" % (label + ":", fmt % result[field]))
    lines.append("")
    if result["alarm"]:
        lines.append(CLOSING.get(result["reason"], "Treat this as a failure."))
        lines.append("This alarm repeats on every scheduled run until the cause is fixed.")
    else:
        lines.append("This proof-of-life message shows that the alert path still works. It arrives "
                     "once a week; if it stops arriving, treat that as a fault.")
    return subject, "\n".join(lines) + "\n"


# ------------------------------------------------------------ alert channels

def _email_config(env):
    """None when email is not configured at all; ConfigError when it is half-configured."""
    names = ("TEM_PROJECT_ID", "TEM_API_KEY", "ALERT_FROM_EMAIL", "ALERT_TO_EMAIL")
    values = {name: _get(env, name) for name in names}
    if not any(values.values()):
        return None
    missing = [name for name in names if not values[name]]
    if missing:
        raise ConfigError("email alerts are half-configured: set %s" % ", ".join(missing))
    recipients = [a.strip() for a in values["ALERT_TO_EMAIL"].split(",") if a.strip()]
    return dict(values, recipients=recipients)


def send_email(env, subject, text):
    """Send through Scaleway Transactional Email (TEM)."""
    cfg = _email_config(env)
    payload = {
        "from": {"email": cfg["ALERT_FROM_EMAIL"], "name": _get(env, "ALERT_FROM_NAME", "Backup Sentinel")},
        "to": [{"email": address} for address in cfg["recipients"]],
        "subject": subject,
        "text": text,
        "project_id": cfg["TEM_PROJECT_ID"],
    }
    request = urllib.request.Request(
        TEM_URL.format(region=_get(env, "TEM_REGION", "fr-par")),
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Auth-Token": cfg["TEM_API_KEY"], "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST")
    try:
        with _urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        excerpt = _error_body(exc).decode("utf-8", "replace")[:300]
        raise NotifyError("Scaleway TEM returned HTTP %s: %s" % (exc.code, excerpt))
    except urllib.error.URLError as exc:
        raise NotifyError("Scaleway TEM unreachable: %s" % exc.reason)


def send_webhook(env, subject, text, result):
    """POST the result as JSON. Error messages never include the URL, which often embeds a secret."""
    url = _get(env, "WEBHOOK_URL")
    if not url.lower().startswith(("https://", "http://")):
        raise NotifyError("WEBHOOK_URL must start with https:// or http://")
    payload = {
        "source": "serverless-backup-sentinel",
        "version": __version__,
        "alarm": result["alarm"],
        "reason": result["reason"],
        "subject": subject,
        "text": text,
        "result": result,
    }
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    token = _get(env, "WEBHOOK_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
        with _urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        _error_body(exc)
        raise NotifyError("the webhook returned HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        raise NotifyError("the webhook is unreachable: %s" % type(exc.reason).__name__)
    except Exception as exc:
        raise NotifyError("the webhook call failed: %s" % type(exc).__name__)


def notify(env, subject, text, result):
    """Send on every configured channel. Returns (channels that took it, {channel: error})."""
    channels, errors = [], {}
    try:
        if _email_config(env) is not None:
            channels.append(("email", lambda: send_email(env, subject, text)))
    except ConfigError as exc:
        errors["email"] = str(exc)
    if _get(env, "WEBHOOK_URL"):
        channels.append(("webhook", lambda: send_webhook(env, subject, text, result)))
    if not channels and not errors:
        errors["config"] = ("no alert channel is configured: set TEM_PROJECT_ID, TEM_API_KEY, "
                            "ALERT_FROM_EMAIL and ALERT_TO_EMAIL, or WEBHOOK_URL, or both")
    sent = []
    for name, send in channels:
        try:
            send()
            sent.append(name)
        except Exception as exc:
            errors[name] = _describe(exc)
    return sent, errors


# ------------------------------------------------------------ entry points

def _proof_of_life_today(env, now):
    try:
        return parse_weekday(_get(env, "PROOF_OF_LIFE_DAY", "monday")) == now.weekday()
    except ConfigError:
        return False  # evaluate() has already turned this into a misconfigured alarm


def _log(record):
    print(json.dumps(dict({"event": "backup-sentinel"}, **record)), flush=True)


def run(trigger, force_notify=False, env=None, now=None):
    """Check once and notify as the trigger requires.

    trigger "cron": alarm on any failure; on PROOF_OF_LIFE_DAY (UTC), report health too.
    trigger "manual": notify only when force_notify is set, healthy or not.
    """
    env = os.environ if env is None else env
    now = now or _utcnow()
    result = evaluate(env, now)
    if trigger == "cron":
        should_notify = result["alarm"] or _proof_of_life_today(env, now)
    else:
        should_notify = force_notify
    sent, errors = [], {}
    if should_notify:
        subject, text = render(result)
        sent, errors = notify(env, subject, text, result)
    record = dict(result, trigger=trigger, notified=sent, notify_errors=errors)
    _log(record)
    return record


def _http_method(event):
    """httpMethod (Scaleway, API Gateway REST) or requestContext.http.method (Lambda function URLs)."""
    method = event.get("httpMethod")
    if not method:
        context = event.get("requestContext")
        http = context.get("http") if isinstance(context, dict) else None
        method = http.get("method") if isinstance(http, dict) else None
    return method.upper() if isinstance(method, str) and method else None


def _headers(event):
    raw = event.get("headers")
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): v for k, v in raw.items() if isinstance(v, str)}


def _json_body(event):
    body = event.get("body")
    if isinstance(body, dict):
        return body
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        if not body:
            return {}
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        data = json.loads(body)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _same(given, expected):
    """Constant-time comparison that does not raise on non-ASCII input."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _is_cron_call(event, env):
    """A Scaleway CRON trigger arrives as an HTTP POST to /, its arguments as the JSON body, and
    no Authorization header. Its arguments carry {"cron_token": CRON_TOKEN}. Anyone who can read
    the trigger can read that value, and all it allows is a run the cron would make anyway."""
    expected = _get(env, "CRON_TOKEN")
    if not expected or _http_method(event) != "POST":
        return False
    given = _json_body(event).get("cron_token")
    return isinstance(given, str) and _same(given, expected)


def _is_authorised(event, env):
    expected = _get(env, "SENTINEL_TOKEN")
    if not expected:
        return False
    scheme, _, token = (_headers(event).get("authorization") or "").partition(" ")
    return scheme.lower() == "bearer" and _same(token.strip(), expected)


CRON_REPLY = ("trigger", "reason", "alarm", "notified", "notify_errors")


def handler(event, context=None):
    """The function entry point.

    * An event with no HTTP shape comes from the platform's own scheduler: run as the cron.
    * A POST whose JSON body carries cron_token equal to CRON_TOKEN: run as the cron.
    * A call with "Authorization: Bearer <SENTINEL_TOKEN>": a manual check; ?notify=1 sends it.
    * Anything else: 401, and the storage is not read.
    """
    env = os.environ
    if not isinstance(event, dict):
        event = {}
    method = _http_method(event)
    is_http = method is not None or "headers" in event or "requestContext" in event

    if not is_http or _is_cron_call(event, env):
        record = run("cron", env=env)
        # Anyone who can read the trigger can read the cron token, so a cron call is told only
        # the outcome. The full result goes to the function's log.
        reply = {name: record[name] for name in CRON_REPLY}
    elif _is_authorised(event, env):
        query = event.get("queryStringParameters")
        notify_now = isinstance(query, dict) and query.get("notify") == "1"
        record = reply = run("manual", force_notify=notify_now, env=env)
    else:
        # Logged so that a misconfigured cron shows up in the function's logs as refused calls.
        _log({"refused": True, "method": method, "path": event.get("path"),
              "has_authorization": "authorization" in _headers(event),
              "body_has_cron_token": "cron_token" in _json_body(event)})
        return {"statusCode": 401, "headers": {"Content-Type": "text/plain; charset=utf-8"},
                "body": "unauthorised\n"}

    return {"statusCode": 502 if record["notify_errors"] else 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(reply)}


handle = handler  # Scaleway's examples name the handler "handle"; either name works.


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="handler.py",
        description="Check that a recent backup exists. Configuration comes from environment "
                    "variables; see README.md. Exit status: 0 healthy, 1 alarm, 2 an alert "
                    "could not be sent.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cron", action="store_true",
                      help="behave as the scheduled run: alarm on failure, proof-of-life on its day")
    mode.add_argument("--notify", action="store_true",
                      help="send the result now, healthy or not")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    record = run("cron" if args.cron else "manual", force_notify=args.notify)
    if record["notify_errors"]:
        return 2
    return 1 if record["alarm"] else 0


if __name__ == "__main__":
    sys.exit(main())
