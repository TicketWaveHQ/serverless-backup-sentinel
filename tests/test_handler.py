"""Offline tests for serverless-backup-sentinel.

No credentials and no network: handler._urlopen is replaced with a fake network,
so these tests drive the real SigV4 signing, XML parsing, pagination, retries and
notification code. Only the far end of each connection is simulated.

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

import ast
import base64
import contextlib
import datetime
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
from unittest import mock
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import handler  # noqa: E402

NOW = datetime.datetime(2026, 11, 9, 9, 30, tzinfo=datetime.timezone.utc)
TODAY = NOW.weekday()
OTHER_DAY = (TODAY + 1) % 7
KEY = "backups/daily/db-2026-11-09.tar.gz.age"

BASE_ENV = {
    "S3_ENDPOINT": "https://s3.fr-par.scw.cloud",
    "S3_REGION": "fr-par",
    "S3_BUCKET": "my-backups-bucket",
    "S3_PREFIX": "backups/daily/",
    "S3_ACCESS_KEY_ID": "EXAMPLEACCESSKEY",
    "S3_SECRET_ACCESS_KEY": "example-secret-access-key",
    "MAX_AGE_HOURS": "26",
    "PROOF_OF_LIFE_DAY": str(OTHER_DAY),  # healthy runs are silent unless a test says otherwise
    "TEM_PROJECT_ID": "example-project-id",
    "TEM_API_KEY": "example-tem-key",
    "ALERT_FROM_EMAIL": "sentinel@example.com",
    "ALERT_TO_EMAIL": "alerts@example.com",
    "SENTINEL_TOKEN": "example-manual-token",
    "CRON_TOKEN": "example-cron-token",
}
SECRETS = ("example-secret-access-key", "example-tem-key", "example-manual-token",
           "example-cron-token", "example-webhook-token", "secret-path")
WEBHOOK_URL = "https://hooks.example.com/secret-path"


def obj(hours, key=KEY, size=4096):
    """One listed object, uploaded `hours` before NOW, its LastModified written as S3 writes it."""
    stamp = (NOW - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"key": key, "last_modified": stamp, "size": size}


def listing_xml(objects, next_token=None, namespace=True):
    ns = ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/"' if namespace else ""
    contents = "".join(
        "<Contents><Key>%s</Key><LastModified>%s</LastModified><ETag>&quot;0&quot;</ETag>%s"
        "<StorageClass>STANDARD</StorageClass></Contents>"
        % (escape(o["key"]), o["last_modified"],
           "" if o["size"] is None else "<Size>%d</Size>" % o["size"])
        for o in objects)
    token = "<NextContinuationToken>%s</NextContinuationToken>" % next_token if next_token else ""
    return ('<?xml version="1.0" encoding="UTF-8"?><ListBucketResult%s><Name>my-backups-bucket</Name>'
            '<Prefix>backups/daily/</Prefix><KeyCount>%d</KeyCount><MaxKeys>1000</MaxKeys>'
            '<IsTruncated>%s</IsTruncated>%s%s</ListBucketResult>'
            % (ns, len(objects), "true" if next_token else "false", contents, token)).encode()


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://example.invalid", code, "error", {}, io.BytesIO(body))


S3_DENIED = b"<?xml version='1.0' encoding='UTF-8'?><Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"


class FakeNet:
    """Stands in for urllib.request.urlopen and records every request it is given."""

    def __init__(self):
        self.calls = []
        self.pages = [[]]        # S3 listing pages, served by continuation token
        self.s3_failures = []    # raised (or returned as a raw body) by the next S3 calls, in order
        self.tem_error = None
        self.webhook_error = None

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        host = urllib.parse.urlsplit(request.full_url).netloc
        if host == "api.scaleway.com":
            if self.tem_error:
                raise self.tem_error
            return io.BytesIO(b'{"emails": []}')
        if host == "hooks.example.com":
            if self.webhook_error:
                raise self.webhook_error
            return io.BytesIO(b"ok")
        if self.s3_failures:
            failure = self.s3_failures.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            return io.BytesIO(failure)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))
        index = int(query.get("continuation-token", "page-0").split("-")[1])
        next_token = "page-%d" % (index + 1) if index + 1 < len(self.pages) else None
        return io.BytesIO(listing_xml(self.pages[index], next_token))

    def _to(self, host):
        return [r for r in self.calls if urllib.parse.urlsplit(r.full_url).netloc == host]

    def s3_requests(self):
        return [r for r in self.calls
                if urllib.parse.urlsplit(r.full_url).netloc not in ("api.scaleway.com", "hooks.example.com")]

    def emails(self):
        return [json.loads(r.data) for r in self._to("api.scaleway.com")]

    def webhooks(self):
        return [r for r in self._to("hooks.example.com")]


class SentinelTestCase(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.sleeps = []
        for patch in (mock.patch.dict(os.environ, BASE_ENV, clear=True),
                      mock.patch.object(handler, "_urlopen", self.net),
                      mock.patch.object(handler, "_sleep", self.sleeps.append),
                      mock.patch.object(handler, "_utcnow", lambda: NOW)):
            patch.start()
            self.addCleanup(patch.stop)
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def env(self, **values):
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def backups(self, *objects):
        self.net.pages = [list(objects)]

    def logs(self):
        return [json.loads(line) for line in self.stdout.getvalue().splitlines() if line.strip()]


def body(response):
    return json.loads(response["body"])


def cron_event(payload='{"cron_token": "example-cron-token"}', b64=False):
    """What a Scaleway CRON trigger delivers: a POST to /, its arguments as the JSON body,
    and no Authorization header."""
    return {"httpMethod": "POST", "path": "/", "headers": {"Content-Type": "application/json"},
            "queryStringParameters": {}, "isBase64Encoded": b64,
            "body": base64.b64encode(payload.encode()).decode() if b64 else payload}


def manual_event(token="example-manual-token", notify=False, method="GET", scheme="Bearer"):
    headers = {"Authorization": "%s %s" % (scheme, token)} if token is not None else {}
    return {"httpMethod": method, "path": "/", "headers": headers, "body": "",
            "queryStringParameters": {"notify": "1"} if notify else {}, "isBase64Encoded": False}


# ------------------------------------------------------------------ signing

class SigV4Tests(SentinelTestCase):
    def cfg(self, **overrides):
        return dict(handler.load_config(os.environ), **overrides)

    def test_matches_the_aws_documented_example(self):
        # The "GET Bucket (List Objects)" example in the Amazon S3 documentation for
        # Signature Version 4 with the Authorization header.
        headers = handler.sign_request(
            "GET", "examplebucket.s3.amazonaws.com", "/", {"max-keys": "2", "prefix": "J"},
            "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
            datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc))
        self.assertTrue(headers["Authorization"].endswith(
            "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"))
        self.assertIn("Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request",
                      headers["Authorization"])
        self.assertEqual(headers["x-amz-date"], "20130524T000000Z")

    def test_query_is_sorted_and_strictly_encoded(self):
        self.assertEqual(handler.canonical_query({"prefix": "backups/daily 1/", "list-type": "2",
                                                  "continuation-token": "a+b/c="}),
                         "continuation-token=a%2Bb%2Fc%3D&list-type=2&prefix=backups%2Fdaily%201%2F")

    def test_path_style_request(self):
        request = handler.build_list_request(self.cfg(), now=NOW)
        self.assertEqual(request.full_url, "https://s3.fr-par.scw.cloud/my-backups-bucket"
                                           "?list-type=2&max-keys=1000&prefix=backups%2Fdaily%2F")
        self.assertEqual(request.get_header("Host"), "s3.fr-par.scw.cloud")
        self.assertEqual(request.get_header("X-amz-content-sha256"), handler.EMPTY_SHA256)
        self.assertIn("/20261109/fr-par/s3/aws4_request", request.get_header("Authorization"))
        self.assertNotIn("example-secret-access-key", request.get_header("Authorization"))

    def test_virtual_hosted_request(self):
        request = handler.build_list_request(self.cfg(addressing="virtual"), now=NOW)
        self.assertTrue(request.full_url.startswith(
            "https://my-backups-bucket.s3.fr-par.scw.cloud/?list-type=2"))
        self.assertEqual(request.get_header("Host"), "my-backups-bucket.s3.fr-par.scw.cloud")

    def test_endpoint_port_is_part_of_the_signed_host(self):
        request = handler.build_list_request(self.cfg(endpoint="http://localhost:9000"), now=NOW)
        self.assertEqual(request.get_header("Host"), "localhost:9000")
        self.assertTrue(request.full_url.startswith("http://localhost:9000/my-backups-bucket?"))

    def test_continuation_token_is_sent(self):
        request = handler.build_list_request(self.cfg(), continuation_token="tok/en=", now=NOW)
        self.assertIn("continuation-token=tok%2Fen%3D", request.full_url)


# ------------------------------------------------------------------ parsing

class ParseTests(unittest.TestCase):
    def test_namespaced_listing(self):
        objects, token = handler.parse_list_response(listing_xml([obj(1)], "next"))
        self.assertEqual(objects[0]["key"], KEY)
        self.assertEqual(objects[0]["size"], 4096)
        self.assertEqual(token, "next")

    def test_listing_without_namespace(self):
        objects, token = handler.parse_list_response(listing_xml([obj(1)], namespace=False))
        self.assertEqual((len(objects), token), (1, None))

    def test_truncated_without_token_cannot_verify(self):
        xml = listing_xml([obj(1)]).replace(b"<IsTruncated>false", b"<IsTruncated>true")
        with self.assertRaises(handler.StorageError):
            handler.parse_list_response(xml)

    def test_not_xml_cannot_verify(self):
        with self.assertRaises(handler.StorageError):
            handler.parse_list_response(b"<html>proxy error")

    def test_error_document_cannot_verify(self):
        with self.assertRaises(handler.StorageError):
            handler.parse_list_response(S3_DENIED)

    def test_timestamp_forms(self):
        want = datetime.datetime(2026, 11, 9, 9, 30, tzinfo=datetime.timezone.utc)
        for text in ("2026-11-09T09:30:00.000Z", "2026-11-09T09:30:00Z", "2026-11-09T09:30:00",
                     "2026-11-09T09:30:00+00:00", "2026-11-09T10:30:00+01:00",
                     "2026-11-09T04:30:00-0500"):
            self.assertEqual(handler.parse_timestamp(text), want, text)
        self.assertEqual(handler.parse_timestamp("2026-11-09T09:30:00.5Z").microsecond, 500000)

    def test_unreadable_timestamp_raises(self):
        for text in ("yesterday", "", None, "2026-11-09 09:30:00"):
            with self.assertRaises(ValueError):
                handler.parse_timestamp(text)


# ------------------------------------------------------------------ the check

class EvaluateTests(SentinelTestCase):
    def check(self, **env):
        self.env(**env)
        return handler.evaluate()

    def test_fresh_backup_is_ok(self):
        self.backups(obj(1))
        result = self.check()
        self.assertEqual((result["reason"], result["alarm"]), ("ok", False))
        self.assertEqual((result["key"], result["age_hours"], result["size_bytes"]), (KEY, 1.0, 4096))

    def test_exactly_at_the_limit_is_ok(self):
        self.backups(obj(26))
        self.assertEqual(self.check()["reason"], "ok")

    def test_just_over_the_limit_is_stale(self):
        self.backups(obj(26.1))
        result = self.check()
        self.assertEqual((result["reason"], result["alarm"]), ("stale", True))
        self.assertIn("26.1 hours old", result["detail"])

    def test_the_newest_backup_decides_whatever_the_listing_order(self):
        old, new = obj(40, "backups/daily/a.tar.gz.age"), obj(2, "backups/daily/b.tar.gz.age")
        for order in ((old, new), (new, old)):
            self.backups(*order)
            result = self.check()
            self.assertEqual((result["reason"], result["key"]), ("ok", new["key"]))

    def test_a_checksum_sidecar_is_not_a_backup(self):
        self.backups(obj(1, KEY + ".sha256"), obj(40))
        result = self.check()
        self.assertEqual((result["reason"], result["key"]), ("stale", KEY))
        self.backups(obj(1, KEY + ".sha256"))
        self.assertEqual(self.check()["reason"], "storage-empty")

    def test_a_plaintext_archive_does_not_match_the_default_suffix(self):
        self.backups(obj(1, "backups/daily/db.tar.gz"))
        result = self.check()
        self.assertEqual(result["reason"], "storage-empty")
        self.assertIn(".tar.gz.age", result["detail"])

    def test_a_suffix_list(self):
        self.backups(obj(1, "backups/daily/db.dump.age"), obj(1, "backups/daily/db.log"))
        result = self.check(BACKUP_SUFFIX=".dump, .dump.age")
        self.assertEqual((result["reason"], result["matching_objects"]), ("ok", 1))

    def test_star_counts_any_key_but_not_a_folder_marker(self):
        self.backups(obj(0, "backups/daily/"), obj(5, "backups/daily/anything.bin"))
        result = self.check(BACKUP_SUFFIX="*")
        self.assertEqual((result["reason"], result["key"]), ("ok", "backups/daily/anything.bin"))

    def test_an_empty_suffix_falls_back_to_the_safe_default(self):
        self.backups(obj(5, "backups/daily/anything.bin"))
        for value in ("", " ", ","):
            result = self.check(BACKUP_SUFFIX=value)
            self.assertEqual((result["reason"], result["suffix"]), ("storage-empty", ".tar.gz.age"))

    def test_empty_storage_is_an_alarm(self):
        result = self.check()
        self.assertEqual((result["reason"], result["alarm"], result["objects_seen"]),
                         ("storage-empty", True, 0))

    def test_the_prefix_is_what_gets_listed(self):
        self.check(S3_PREFIX="")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(
            self.net.s3_requests()[0].full_url).query, keep_blank_values=True))
        self.assertEqual(query["prefix"], "")
        self.check(S3_PREFIX="backups/weekly/")
        self.assertIn("prefix=backups%2Fweekly%2F", self.net.s3_requests()[1].full_url)

    def test_every_page_is_read(self):
        self.net.pages = [[obj(40, "backups/daily/a.tar.gz.age")], [obj(2, "backups/daily/z.tar.gz.age")]]
        result = self.check()
        self.assertEqual((result["reason"], result["objects_seen"]), ("ok", 2))
        requests = self.net.s3_requests()
        self.assertEqual(len(requests), 2)
        self.assertIn("continuation-token=page-1", requests[1].full_url)

    def test_more_pages_than_the_cap_cannot_verify(self):
        self.net.pages = [[obj(1)]] * 5
        result = self.check(S3_MAX_PAGES="2")
        self.assertEqual(result["reason"], "storage-unreadable")
        self.assertIn("S3_MAX_PAGES", result["detail"])
        self.assertEqual(len(self.net.s3_requests()), 2)

    def test_access_denied_is_an_alarm_and_is_not_retried(self):
        self.net.s3_failures = [http_error(403, S3_DENIED)]
        result = self.check()
        self.assertEqual((result["reason"], result["alarm"]), ("storage-unreadable", True))
        self.assertIn("HTTP 403 (AccessDenied)", result["detail"])
        self.assertEqual((len(self.net.s3_requests()), self.sleeps), (1, []))

    def test_a_transient_error_is_retried(self):
        self.net.s3_failures = [http_error(503)]
        self.backups(obj(1))
        self.assertEqual(self.check()["reason"], "ok")
        self.assertEqual((len(self.net.s3_requests()), self.sleeps), (2, [1]))

    def test_throttling_is_retried(self):
        self.net.s3_failures = [http_error(429)]
        self.backups(obj(1))
        self.assertEqual(self.check()["reason"], "ok")

    def test_a_persistent_outage_alarms_after_every_attempt(self):
        self.net.s3_failures = [http_error(500)] * 3
        result = self.check()
        self.assertEqual(result["reason"], "storage-unreadable")
        self.assertEqual((len(self.net.s3_requests()), self.sleeps), (3, [1, 2]))

    def test_attempts_are_configurable(self):
        self.net.s3_failures = [http_error(500)]
        self.assertEqual(self.check(S3_ATTEMPTS="1")["reason"], "storage-unreadable")
        self.assertEqual(len(self.net.s3_requests()), 1)

    def test_network_failures_are_alarms(self):
        for failure in (urllib.error.URLError("Name or service not known"), TimeoutError("timed out")):
            self.net.s3_failures = [failure] * 3
            result = self.check()
            self.assertEqual(result["reason"], "storage-unreadable")
            self.assertIn(type(failure).__name__, result["detail"])

    def test_a_response_that_is_not_a_listing_is_an_alarm(self):
        self.net.s3_failures = [b"<html>gateway</html>"] * 3
        self.assertEqual(self.check()["reason"], "storage-unreadable")

    def test_an_unreadable_timestamp_is_an_alarm(self):
        broken = dict(obj(1), last_modified="last tuesday")
        self.backups(broken)
        result = self.check()
        self.assertEqual(result["reason"], "storage-unreadable")
        self.assertIn("timestamp", result["detail"])

    def test_a_zero_byte_backup_is_too_small(self):
        self.backups(obj(1, size=0))
        result = self.check()
        self.assertEqual((result["reason"], result["alarm"]), ("too-small", True))

    def test_the_minimum_size_is_configurable(self):
        self.backups(obj(1, size=500))
        self.assertEqual(self.check(MIN_SIZE_BYTES="1000")["reason"], "too-small")
        self.backups(obj(1, size=0))
        self.assertEqual(self.check(MIN_SIZE_BYTES="0")["reason"], "ok")

    def test_a_listing_without_sizes_cannot_verify_the_size(self):
        self.backups(obj(1, size=None))
        self.assertEqual(self.check()["reason"], "storage-unreadable")
        self.assertEqual(self.check(MIN_SIZE_BYTES="0")["reason"], "ok")

    def test_missing_storage_settings_are_an_alarm(self):
        result = self.check(S3_BUCKET=None, S3_SECRET_ACCESS_KEY="  ")
        self.assertEqual((result["reason"], result["alarm"]), ("misconfigured", True))
        self.assertIn("S3_BUCKET, S3_SECRET_ACCESS_KEY", result["detail"])
        self.assertEqual(self.net.s3_requests(), [])

    def test_invalid_numbers_are_an_alarm(self):
        for name, value in (("MAX_AGE_HOURS", "a day"), ("MAX_AGE_HOURS", "nan"),
                            ("MAX_AGE_HOURS", "inf"), ("MAX_AGE_HOURS", "-1"),
                            ("MIN_SIZE_BYTES", "1.5"), ("S3_ATTEMPTS", "0"), ("S3_MAX_PAGES", "0")):
            with self.subTest(name=name, value=value):
                os.environ.update(BASE_ENV)
                self.assertEqual(self.check(**{name: value})["reason"], "misconfigured")

    def test_other_invalid_settings_are_an_alarm(self):
        for name, value in (("PROOF_OF_LIFE_DAY", "7"), ("PROOF_OF_LIFE_DAY", "someday"),
                            ("S3_ENDPOINT", "s3.fr-par.scw.cloud"), ("S3_ADDRESSING", "dns")):
            with self.subTest(name=name, value=value):
                os.environ.update(BASE_ENV)
                self.assertEqual(self.check(**{name: value})["reason"], "misconfigured")

    def test_the_label_defaults_to_bucket_and_prefix(self):
        self.backups(obj(1))
        self.assertEqual(self.check()["name"], "my-backups-bucket/backups/daily/")
        self.assertEqual(self.check(SENTINEL_NAME="nightly database")["name"], "nightly database")

    def test_results_never_carry_secrets(self):
        self.net.s3_failures = [http_error(403, S3_DENIED)]
        results = [self.check()]
        self.backups(obj(1))
        results.append(self.check())
        results.append(self.check(S3_BUCKET=None))
        for result in results:
            for secret in SECRETS:
                self.assertNotIn(secret, json.dumps(result))


class WeekdayTests(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(handler.parse_weekday("monday"), 0)
        self.assertEqual(handler.parse_weekday("Mon"), 0)
        self.assertEqual(handler.parse_weekday(" SUNDAY "), 6)
        self.assertEqual(handler.parse_weekday("3"), 3)

    def test_invalid_days(self):
        for value in ("7", "-1", "", None, "mo", "funday"):
            with self.assertRaises(handler.ConfigError):
                handler.parse_weekday(value)


# ------------------------------------------------------------------ the real cron shape

class CronShapeTests(SentinelTestCase):
    """A Scaleway CRON trigger calls the function as an HTTP POST to / with the cron's arguments
    as the JSON body and no Authorization header. A handler that treats every HTTP call as an
    outside caller answers its own cron 401 and sends nothing, while every manual test passes."""

    def test_the_real_cron_shape_runs_as_the_cron(self):
        self.backups(obj(99))
        response = handler.handler(cron_event(), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual((body(response)["trigger"], body(response)["notified"]), ("cron", ["email"]))
        self.assertTrue(self.net.emails()[0]["subject"].startswith("ALARM: backup stale"))

    def test_a_cron_call_is_told_only_the_outcome(self):
        self.backups(obj(1))
        reply = body(handler.handler(cron_event(), None))
        self.assertEqual(set(reply), {"trigger", "reason", "alarm", "notified", "notify_errors"})
        self.assertEqual(self.logs()[-1]["key"], KEY)  # the full result is in the log
        self.assertEqual(body(handler.handler(manual_event(), None))["key"], KEY)

    def test_a_base64_body(self):
        self.backups(obj(99))
        response = handler.handler(cron_event(b64=True), None)
        self.assertEqual(body(response)["notified"], ["email"])

    def test_a_cron_without_its_token_is_refused_and_says_so_in_the_log(self):
        response = handler.handler(cron_event("{}"), None)
        self.assertEqual(response["statusCode"], 401)
        log = self.logs()[-1]
        self.assertEqual((log["refused"], log["method"], log["path"]), (True, "POST", "/"))
        self.assertEqual((log["has_authorization"], log["body_has_cron_token"]), (False, False))

    def test_refused_calls_never_read_storage_or_send(self):
        for payload in ('{"cron_token": "nope"}', "", "example-cron-token", '["example-cron-token"]',
                        '{"cron_token": 12345}', '{"cron_token": null}',
                        '{"cron_token": "example-crön-token"}'):
            with self.subTest(payload=payload):
                self.assertEqual(handler.handler(cron_event(payload), None)["statusCode"], 401)
        self.assertEqual(self.net.calls, [])

    def test_a_body_already_decoded_by_the_platform(self):
        self.backups(obj(99))
        for decoded in ({"cron_token": "example-cron-token"}, b'{"cron_token": "example-cron-token"}'):
            event = dict(cron_event(), body=decoded)
            self.assertEqual(body(handler.handler(event, None))["trigger"], "cron")

    def test_headers_that_are_not_a_mapping_are_ignored(self):
        for headers in (None, [], "Authorization: Bearer example-manual-token"):
            event = dict(manual_event(), headers=headers)
            self.assertEqual(handler.handler(event, None)["statusCode"], 401)

    def test_the_cron_token_only_counts_on_a_post(self):
        event = dict(cron_event(), httpMethod="GET")
        self.assertEqual(handler.handler(event, None)["statusCode"], 401)

    def test_no_cron_token_configured_means_no_cron_over_http(self):
        self.env(CRON_TOKEN=None)
        self.assertEqual(handler.handler(cron_event('{"cron_token": ""}'), None)["statusCode"], 401)
        self.assertEqual(handler.handler(cron_event(), None)["statusCode"], 401)

    def test_the_refusal_log_carries_no_token(self):
        handler.handler(cron_event('{"cron_token": "example-cron-tokem"}'), None)
        handler.handler(manual_event("example-manual-tokem"), None)
        for secret in SECRETS + ("example-cron-tokem", "example-manual-tokem"):
            self.assertNotIn(secret, self.stdout.getvalue())


class ScheduleTests(SentinelTestCase):
    def test_an_event_with_no_http_shape_is_the_platform_scheduler(self):
        self.backups(obj(99))
        for event in ({}, {"source": "aws.scheduler", "detail-type": "Scheduled Event", "detail": {}}):
            response = handler.handler(event, None)
            self.assertEqual((response["statusCode"], body(response)["trigger"]), (200, "cron"))
        self.assertEqual(len(self.net.emails()), 2)

    def test_an_event_that_is_not_a_mapping_is_the_platform_scheduler(self):
        self.backups(obj(1))
        self.assertEqual(body(handler.handler("scheduled", None))["trigger"], "cron")

    def test_a_broken_proof_of_life_day_alarms_once_per_run(self):
        self.backups(obj(1))
        self.env(PROOF_OF_LIFE_DAY="Mondays")
        response = handler.handler(cron_event(), None)
        self.assertEqual(body(response)["reason"], "misconfigured")
        self.assertEqual(len(self.net.emails()), 1)

    def test_healthy_on_the_proof_of_life_day_reports_health(self):
        self.backups(obj(1))
        for day in (str(TODAY), handler.DAYS[TODAY], handler.DAYS[TODAY][:3].upper()):
            self.env(PROOF_OF_LIFE_DAY=day)
            self.assertEqual(body(handler.handler(cron_event(), None))["notified"], ["email"], day)
        self.assertTrue(all(m["subject"].startswith("OK: backups healthy") for m in self.net.emails()))

    def test_healthy_on_any_other_day_is_silent(self):
        self.backups(obj(1))
        response = handler.handler(cron_event(), None)
        self.assertEqual((response["statusCode"], body(response)["notified"]), (200, []))
        self.assertEqual(self.net.emails(), [])

    def test_the_default_proof_of_life_day_is_monday(self):
        self.env(PROOF_OF_LIFE_DAY=None)
        self.backups(obj(1))
        handler.handler(cron_event(), None)
        self.assertEqual(len(self.net.emails()), 1 if TODAY == 0 else 0)

    def test_an_alarm_is_sent_on_any_day(self):
        self.net.s3_failures = [http_error(403, S3_DENIED)]
        handler.handler(cron_event(), None)
        self.assertTrue(self.net.emails()[0]["subject"].startswith("ALARM: cannot verify backups"))

    def test_a_misconfigured_sentinel_alarms(self):
        self.env(S3_BUCKET=None)
        handler.handler(cron_event(), None)
        self.assertTrue(self.net.emails()[0]["subject"].startswith("ALARM: backup sentinel misconfigured"))


class ManualCallTests(SentinelTestCase):
    def test_a_manual_check_reports_without_sending(self):
        self.backups(obj(99))
        response = handler.handler(manual_event(), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual((body(response)["reason"], body(response)["trigger"]), ("stale", "manual"))
        self.assertEqual(self.net.emails(), [])

    def test_notify_sends_health(self):
        self.backups(obj(1))
        handler.handler(manual_event(notify=True), None)
        self.assertTrue(self.net.emails()[0]["subject"].startswith("OK: backups healthy"))

    def test_notify_sends_an_alarm(self):
        self.backups(obj(99))
        handler.handler(manual_event(notify=True), None)
        self.assertTrue(self.net.emails()[0]["subject"].startswith("ALARM: backup stale"))

    def test_the_scheme_is_case_insensitive(self):
        self.assertEqual(handler.handler(manual_event(scheme="bearer"), None)["statusCode"], 200)

    def test_bad_credentials_are_refused_without_reading_storage(self):
        for event in (manual_event("wrong"), manual_event(None), manual_event(scheme="Basic"),
                      manual_event("example-manual-töken"), manual_event("")):
            self.assertEqual(handler.handler(event, None)["statusCode"], 401)
        self.assertEqual(self.net.calls, [])

    def test_no_manual_token_configured_means_no_manual_calls(self):
        self.env(SENTINEL_TOKEN=None)
        self.assertEqual(handler.handler(manual_event(""), None)["statusCode"], 401)

    def test_a_lambda_function_url_call_is_http_not_a_scheduler_event(self):
        event = {"requestContext": {"http": {"method": "GET"}}, "headers": {}, "rawPath": "/"}
        self.assertEqual(handler.handler(event, None)["statusCode"], 401)
        event["headers"] = {"authorization": "Bearer example-manual-token"}
        self.assertEqual(handler.handler(event, None)["statusCode"], 200)

    def test_handle_is_the_same_entry_point(self):
        self.assertIs(handler.handle, handler.handler)


# ------------------------------------------------------------------ alert channels

class ChannelTests(SentinelTestCase):
    def alarm(self):
        self.backups(obj(99))
        return handler.handler(cron_event(), None)

    def test_the_email_request(self):
        self.env(ALERT_TO_EMAIL="alerts@example.com, oncall@example.com", TEM_REGION="nl-ams",
                 ALERT_FROM_NAME="Sentinel")
        self.alarm()
        request = self.net._to("api.scaleway.com")[0]
        self.assertEqual(request.full_url, "https://api.scaleway.com/transactional-email/v1alpha1"
                                           "/regions/nl-ams/emails")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-auth-token"), "example-tem-key")
        mail = self.net.emails()[0]
        self.assertEqual(mail["from"], {"email": "sentinel@example.com", "name": "Sentinel"})
        self.assertEqual(mail["to"], [{"email": "alerts@example.com"}, {"email": "oncall@example.com"}])
        self.assertEqual(mail["project_id"], "example-project-id")
        self.assertIn("The newest backup is 99.0 hours old", mail["text"])

    def test_a_refused_email_is_a_502(self):
        self.net.tem_error = http_error(400, b'{"message": "invalid sender"}')
        response = self.alarm()
        self.assertEqual(response["statusCode"], 502)
        self.assertIn("HTTP 400", body(response)["notify_errors"]["email"])

    def test_an_unreachable_email_api_is_a_502(self):
        self.net.tem_error = urllib.error.URLError("timed out")
        self.assertIn("unreachable", body(self.alarm())["notify_errors"]["email"])

    def test_the_webhook_alone(self):
        self.env(TEM_PROJECT_ID=None, TEM_API_KEY=None, ALERT_FROM_EMAIL=None, ALERT_TO_EMAIL=None,
                 WEBHOOK_URL=WEBHOOK_URL, WEBHOOK_TOKEN="example-webhook-token")
        response = self.alarm()
        self.assertEqual((response["statusCode"], body(response)["notified"]), (200, ["webhook"]))
        self.assertEqual(self.net.emails(), [])
        request = self.net.webhooks()[0]
        self.assertEqual(request.full_url, WEBHOOK_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer example-webhook-token")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        payload = json.loads(request.data)
        self.assertEqual((payload["source"], payload["alarm"], payload["reason"]),
                         ("serverless-backup-sentinel", True, "stale"))
        self.assertTrue(payload["text"].startswith("ALARM: backup stale"))
        self.assertEqual(payload["result"]["key"], KEY)

    def test_the_webhook_without_a_token_sends_no_authorization(self):
        self.env(WEBHOOK_URL=WEBHOOK_URL)
        self.alarm()
        self.assertIsNone(self.net.webhooks()[0].get_header("Authorization"))

    def test_both_channels(self):
        self.env(WEBHOOK_URL=WEBHOOK_URL)
        self.assertEqual(body(self.alarm())["notified"], ["email", "webhook"])

    def test_one_channel_failing_does_not_stop_the_other(self):
        self.env(WEBHOOK_URL=WEBHOOK_URL)
        self.net.tem_error = http_error(500)
        response = self.alarm()
        self.assertEqual(response["statusCode"], 502)
        self.assertEqual(body(response)["notified"], ["webhook"])
        self.assertEqual(list(body(response)["notify_errors"]), ["email"])

    def test_no_channel_at_all_is_a_502(self):
        self.env(TEM_PROJECT_ID=None, TEM_API_KEY=None, ALERT_FROM_EMAIL=None, ALERT_TO_EMAIL=None)
        response = self.alarm()
        self.assertEqual(response["statusCode"], 502)
        self.assertIn("no alert channel", body(response)["notify_errors"]["config"])

    def test_half_configured_email_is_a_502_that_names_what_is_missing(self):
        self.env(TEM_API_KEY=None)
        response = self.alarm()
        self.assertEqual(response["statusCode"], 502)
        self.assertIn("TEM_API_KEY", body(response)["notify_errors"]["email"])

    def test_webhook_errors_never_include_the_url(self):
        cases = ((WEBHOOK_URL, http_error(500), "HTTP 500"),
                 (WEBHOOK_URL, urllib.error.URLError(OSError("refused")), "unreachable"),
                 (WEBHOOK_URL, ValueError("bad url " + WEBHOOK_URL), "failed"),
                 ("hooks.example.com/secret-path", None, "must start with"))
        for url, failure, expected in cases:
            with self.subTest(expected=expected):
                self.env(WEBHOOK_URL=url)
                self.net.webhook_error = failure
                error = body(self.alarm())["notify_errors"]["webhook"]
                self.assertIn(expected, error)
                self.assertNotIn("secret-path", error)
        self.assertNotIn("secret-path", self.stdout.getvalue())


class RenderTests(unittest.TestCase):
    def result(self, reason, **fields):
        base = {"name": "my-backups-bucket/backups/daily/", "detail": "Detail line.",
                "alarm": reason != "ok", "reason": reason}
        return dict(base, **fields)

    def test_every_alarm_reason_has_an_alarm_subject(self):
        for reason in ("stale", "too-small", "storage-empty", "storage-unreadable", "misconfigured",
                       "something-new"):
            subject, text = handler.render(self.result(reason))
            self.assertTrue(subject.startswith("ALARM: "), reason)
            self.assertIn("repeats on every scheduled run", text)

    def test_cannot_verify_says_it_is_a_failure(self):
        _, text = handler.render(self.result("storage-unreadable"))
        self.assertIn("cannot be verified is not a working backup", text)

    def test_the_healthy_message_asks_to_be_missed(self):
        subject, text = handler.render(self.result("ok", key=KEY, age_hours=1.0, size_bytes=4096))
        self.assertEqual(subject, "OK: backups healthy (my-backups-bucket/backups/daily/)")
        self.assertIn("if it stops arriving, treat that as a fault", text)
        self.assertIn("object:   " + KEY, text)
        self.assertIn("age:      1.0 hours", text)
        self.assertIn("size:     4096 bytes", text)


# ------------------------------------------------------------------ command line

class CommandLineTests(SentinelTestCase):
    def test_a_plain_run_reports_and_sends_nothing(self):
        self.backups(obj(1))
        self.assertEqual(handler.main([]), 0)
        self.assertEqual((self.logs()[-1]["reason"], self.logs()[-1]["trigger"]), ("ok", "manual"))
        self.backups(obj(99))
        self.assertEqual(handler.main([]), 1)
        self.assertEqual(self.net.emails(), [])

    def test_cron_mode_alarms(self):
        self.backups(obj(99))
        self.assertEqual(handler.main(["--cron"]), 1)
        self.assertEqual(len(self.net.emails()), 1)

    def test_notify_mode_sends_health(self):
        self.backups(obj(1))
        self.assertEqual(handler.main(["--notify"]), 0)
        self.assertEqual(len(self.net.emails()), 1)

    def test_a_failed_alert_exits_2(self):
        self.backups(obj(99))
        self.net.tem_error = http_error(500)
        self.assertEqual(handler.main(["--cron"]), 2)


class ScriptTests(unittest.TestCase):
    """handler.py run as a script, in a fresh interpreter with an empty environment (no network)."""

    def script(self, *args):
        import subprocess
        return subprocess.run([sys.executable, os.path.join(ROOT, "handler.py")] + list(args),
                              capture_output=True, text=True, env={}, timeout=60)

    def test_version(self):
        done = self.script("--version")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, handler.__version__))

    def test_an_unconfigured_run_is_a_misconfigured_alarm(self):
        done = self.script()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["reason"], "misconfigured")

    def test_the_clock_is_utc(self):
        self.assertEqual(handler._utcnow().utcoffset(), datetime.timedelta(0))


# ------------------------------------------------------------------ hygiene

class HygieneTests(SentinelTestCase):
    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "needs Python 3.10 or later")
    def test_the_handler_imports_only_the_standard_library(self):
        with open(os.path.join(ROOT, "handler.py"), encoding="utf-8") as source:
            tree = ast.parse(source.read())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertEqual(modules - set(sys.stdlib_module_names), set())

    def test_logs_never_carry_secrets(self):
        self.env(WEBHOOK_URL=WEBHOOK_URL, WEBHOOK_TOKEN="example-webhook-token")
        self.backups(obj(99))
        handler.handler(cron_event(), None)
        handler.handler(manual_event(notify=True), None)
        self.net.s3_failures = [http_error(403, S3_DENIED)]
        handler.handler(cron_event(), None)
        output = self.stdout.getvalue()
        self.assertEqual(len(self.logs()), 3)
        for secret in SECRETS:
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
