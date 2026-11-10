import base64
import os
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import requests

os.environ["JOBBOT_PROFILE"] = str(Path(__file__).resolve().parents[1] / "profile.example")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobbot import filters, notify, pipeline, web  # noqa: E402
from jobbot import store as store_module  # noqa: E402
from jobbot.ai import AIError, OpenAIClient  # noqa: E402
from jobbot.cli import within_active_hours  # noqa: E402
from jobbot.config import APP_URL, parse_env  # noqa: E402
from jobbot.google_api import message_text  # noqa: E402
from jobbot.models import Job, html_to_text, normalize_company, normalize_title  # noqa: E402
from jobbot.sources import careers, gmail_alerts, jsearch, manual, public  # noqa: E402
from jobbot.store import DUPLICATE, FILTERED_OUT, MATCHED, NEW, Store  # noqa: E402


def make_job(native_id="1", title="GTM Engineer (m/w/d)", company="Acme GmbH", source="arbeitnow", **kwargs):
    values = {"location": "Berlin", "url": f"https://example.com/{source}/{native_id}", "description": "A real job."}
    values.update(kwargs)
    return Job(source=source, native_id=native_id, title=title, company=company, **values)


def assessment(**overrides):
    values = {
        "worth_applying": True, "fit_score": 80, "summary": "Good fit.", "reasons": ["Automation for sales."],
        "gaps": ["HubSpot."], "unknowns": [], "language_requirement": "none",
        "on_site_days_per_week": None, "salary_max_per_year": None,
    }
    values.update(overrides)
    return values


class FakeAI:
    def __init__(self, result=None, same=False, error=None):
        self.result = result or assessment()
        self.same = same
        self.error = error
        self.assessed = []
        self.compared = []

    def assess_job(self, job, target_role):
        if self.error:
            raise self.error
        self.assessed.append(job["job_id"])
        return self.result

    def same_role(self, first, second):
        self.compared.append((first["job_id"], second["job_id"]))
        return self.same


class FakeGmail:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send_to_owner(self, subject, text, html):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append((subject, text, html))


class HtmlToTextTest(unittest.TestCase):
    def test_text_with_an_ampersand_is_kept(self):
        self.assertEqual(html_to_text("AI Enablement Specialist FP&A"), "AI Enablement Specialist FP&A")
        self.assertEqual(html_to_text("<p>Sales & Marketing</p><p>R&D"), "Sales & Marketing\nR&D")


class NormalisationTest(unittest.TestCase):
    def test_company_ignores_legal_form_and_case(self):
        self.assertEqual(normalize_company("ACME GmbH & Co. KG"), normalize_company("Acme"))

    def test_title_ignores_gender_tags(self):
        for title in ("GTM Engineer (m/w/d)", "GTM Engineer (f/m/x)", "GTM Engineer (all genders)", "gtm engineer"):
            self.assertEqual(normalize_title(title), "gtm engineer")

    def test_title_ignores_bare_gender_tags_and_zero_width_characters(self):
        self.assertEqual(
            normalize_title("Go-to-Market Engineer – Revenue Operations; f​/m​/d"),
            normalize_title("Go-to-Market Engineer – Revenue Operations (f/m/d)"),
        )

    def test_empty_company_never_shares_a_key(self):
        self.assertNotEqual(make_job("1", company="").dedupe_key, make_job("2", company="").dedupe_key)

    def test_html_to_text_keeps_links_when_asked(self):
        text = html_to_text('<p>Role <a href="https://x.test/1">Apply</a></p><script>bad()</script>', keep_links=True)
        self.assertIn("[https://x.test/1]", text)
        self.assertNotIn("bad()", text)

    def test_env_parser(self):
        self.assertEqual(parse_env("# note\nOPENAI_KEY='abc'\nEMPTY=\n"), {"OPENAI_KEY": "abc", "EMPTY": ""})


class FilterTest(unittest.TestCase):
    def test_target_titles_match(self):
        for title in (
            "GTM Engineer", "Go-to-Market Engineer - Revenue Operations (f/m/d)", "Revenue Operations Engineer | AI & Automation",
            "RevOps Specialist", "AI Automation Engineer", "CRM & Automation Engineer (AI-Driven RevOps)", "Business Systems Engineer",
            "KI-Automatisierung Manager", "AI Enablement Lead", "Founding GTM Engineer", "Marketing Automation Manager",
        ):
            self.assertTrue(filters.is_target_title(title), title)

    def test_other_titles_do_not_match(self):
        for title in (
            "Steuerberater (m/w/d)", "Senior Backend Engineer", "Data Scientist", "Werkstudent Revenue Operations",
            "QA Automation Engineer", "Test Automation Engineer", "Automatisierungstechniker SPS", "Account Executive",
        ):
            self.assertFalse(filters.is_target_title(title), title)

    def test_location_rules(self):
        self.assertTrue(filters.is_in_search_area(make_job(location="Berlin, Germany")))
        self.assertTrue(filters.is_in_search_area(make_job(location="Hamburg", remote=True)))
        self.assertTrue(filters.is_in_search_area(make_job(location="")))
        self.assertFalse(filters.is_in_search_area(make_job(location="Hamburg")))

    def test_recent_means_within_48_hours(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(filters.is_recent(make_job(posted_at="2026-11-07T12:00:00+00:00"), now))
        self.assertFalse(filters.is_recent(make_job(posted_at="2026-11-07T11:59:00+00:00"), now))
        self.assertTrue(filters.is_recent(make_job(posted_at=""), now))
        self.assertTrue(filters.is_recent(make_job(posted_at="2026-11-08T00:00:00.000Z"), now))
        self.assertFalse(filters.passes_rules(make_job(posted_at="2026-11-01T00:00:00+00:00"), now))

    def test_jsearch_allows_72_hours_for_day_level_dates(self):
        now = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
        self.assertTrue(filters.is_recent(make_job(source="jsearch", posted_at="2026-11-07T00:00:00.000Z"), now))
        self.assertFalse(filters.is_recent(make_job(source="jsearch", posted_at="2026-11-06T00:00:00.000Z"), now))
        self.assertFalse(filters.is_recent(make_job(source="arbeitnow", posted_at="2026-11-07T00:00:00.000Z"), now))

    def test_excluded_company_is_skipped_whatever_its_legal_form(self):
        with mock.patch.object(filters, "EXCLUDED_COMPANIES", {normalize_company("Acme")}):
            self.assertFalse(filters.passes_rules(make_job(company="ACME GmbH")))
        self.assertTrue(filters.passes_rules(make_job(company="ACME GmbH")))

    def test_hard_requirements(self):
        self.assertIsNone(filters.hard_requirement_failure(assessment()))
        self.assertIn("German", filters.hard_requirement_failure(assessment(language_requirement="required")))
        self.assertIn("5 days", filters.hard_requirement_failure(assessment(on_site_days_per_week=5)))
        self.assertIsNone(filters.hard_requirement_failure(assessment(on_site_days_per_week=4)))
        self.assertIn("59,000", filters.hard_requirement_failure(assessment(salary_max_per_year=59000)))
        self.assertIsNone(filters.hard_requirement_failure(assessment(salary_max_per_year=60000)))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_same_id_is_seen_once(self):
        self.assertEqual(self.store.add(make_job()), "new")
        self.assertEqual(self.store.add(make_job()), "seen")

    def test_same_job_on_another_source_keeps_both_links(self):
        self.store.add(make_job("1", url="https://a.test/1"))
        result = self.store.add(make_job("9", source="berlinstartupjobs", company="ACME", title="GTM Engineer", url="https://b.test/9"))
        self.assertEqual(result, "duplicate")
        self.assertEqual(json.loads(self.store.get("arbeitnow:1")["other_links"]), ["https://b.test/9"])
        self.assertEqual(self.store.get("berlinstartupjobs:9")["status"], DUPLICATE)

    def test_later_description_fills_in_a_blank(self):
        self.store.add(make_job(description=""))
        self.store.add(make_job(description="Now known."))
        self.assertEqual(self.store.get("arbeitnow:1")["description"], "Now known.")


class SourceParsingTest(unittest.TestCase):
    def test_arbeitnow(self):
        job = public.parse_arbeitnow({
            "slug": "abc", "title": "GTM Engineer", "company_name": "Acme", "location": "Berlin",
            "url": "https://x.test", "description": "<p>Hello</p>", "remote": False, "created_at": 1794205859,
        })
        self.assertEqual((job.job_id, job.description, job.posted_at), ("arbeitnow:abc", "Hello", "2026-11-09T06:30:59+00:00"))

    def test_arbeitnow_keeps_earlier_pages_when_rate_limited(self):
        now = datetime.now(timezone.utc).timestamp()
        item = {"slug": "a", "title": "T", "company_name": "C", "url": "https://x.test", "created_at": now}
        first = mock.Mock(**{"json.return_value": {"data": [item]}})
        limited = mock.Mock(**{"raise_for_status.side_effect": requests.HTTPError("429")})
        session = mock.Mock(**{"get.side_effect": [first, limited]})
        with mock.patch("jobbot.sources.public.time.sleep"):
            self.assertEqual(len(public.fetch_arbeitnow(session)), 1)

    def test_arbeitnow_failure_on_first_page_is_reported(self):
        limited = mock.Mock(**{"raise_for_status.side_effect": requests.HTTPError("429")})
        with self.assertRaises(requests.HTTPError):
            public.fetch_arbeitnow(mock.Mock(**{"get.return_value": limited}))

    def test_arbeitnow_stops_after_two_old_pages(self):
        old = {"slug": "a", "title": "T", "company_name": "C", "url": "https://x.test", "created_at": 1_700_000_000}
        session = mock.Mock(**{"get.return_value": mock.Mock(**{"json.return_value": {"data": [old]}})})
        with mock.patch("jobbot.sources.public.time.sleep"):
            public.fetch_arbeitnow(session)
        self.assertEqual(session.get.call_count, 2)

    def test_alert_uses_stated_age_or_email_time(self):
        received = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        message = {"from": "a@linkedin.com", "text": "[https://www.linkedin.com/jobs/view/1] [https://www.linkedin.com/jobs/view/2]", "received_at": received}
        extracted = [
            {"title": "A", "company": "C", "location": "Berlin", "url": "https://www.linkedin.com/jobs/view/1", "days_since_posted": 3},
            {"title": "B", "company": "C", "location": "Berlin", "url": "https://www.linkedin.com/jobs/view/2", "days_since_posted": None},
        ]
        jobs = gmail_alerts.jobs_from_alert(message, extracted)
        self.assertEqual([job.posted_at for job in jobs], ["2026-11-06T12:00:00+00:00", "2026-11-09T12:00:00+00:00"])
        self.assertFalse(filters.is_recent(jobs[0], received))
        self.assertTrue(filters.is_recent(jobs[1], received))

    def test_berlin_startup_jobs_feed(self):
        feed = b"""<rss><channel><item><title>GTM Lead // hypd.ai</title><link>https://x.test/1</link>
        <guid>https://berlinstartupjobs.com/?p=45</guid><pubDate>Fri, 06 Nov 2026 12:23:14 +0000</pubDate>
        <description>&lt;p&gt;About&lt;/p&gt;</description></item></channel></rss>"""
        job = public.parse_berlin_startup_jobs(feed)[0]
        self.assertEqual((job.title, job.company, job.native_id, job.description), ("GTM Lead", "hypd.ai", "45", "About"))

    def test_jsearch_prefers_the_employers_own_link(self):
        item = {
            "job_id": "abc", "job_title": "GTM Engineer", "employer_name": "Acme", "job_city": None,
            "job_location": "Berlin • über Trabajo.org", "job_apply_link": "https://board.test/1",
            "job_apply_is_direct": False, "job_posted_at_datetime_utc": "2026-11-08T00:00:00.000Z",
            "apply_options": [{"apply_link": "https://acme.test/1", "is_direct": True}],
        }
        job = jsearch.parse_job(item)
        self.assertEqual((job.url, job.location, job.job_id), ("https://acme.test/1", "Berlin", "jsearch:abc"))

    def test_jsearch_keeps_results_when_one_query_fails(self):
        good = mock.Mock(**{"json.return_value": {"data": {"jobs": [{"job_id": "a"}]}}})
        bad = mock.Mock(**{"raise_for_status.side_effect": requests.HTTPError("429")})
        session = mock.Mock(**{"get.side_effect": [bad, good] + [good] * (len(jsearch.JSEARCH_QUERIES) - 2)})
        self.assertEqual(len(jsearch.fetch_jsearch(session, "key")), 1)

    def test_jsearch_reports_failure_when_all_queries_fail(self):
        bad = mock.Mock(**{"raise_for_status.side_effect": requests.HTTPError("429")})
        with self.assertRaises(requests.HTTPError):
            jsearch.fetch_jsearch(mock.Mock(**{"get.return_value": bad}), "key")

    def test_jsearch_runs_once_per_slot(self):
        store = Store(":memory:")
        tz = ZoneInfo("Europe/Berlin")
        self.assertFalse(pipeline.jsearch_is_due(store, datetime(2026, 11, 9, 7, 30, tzinfo=tz)))
        morning = datetime(2026, 11, 9, 8, 0, tzinfo=tz)
        self.assertTrue(pipeline.jsearch_is_due(store, morning))
        store.set_meta("jsearch_last_fetch", morning.isoformat())
        self.assertFalse(pipeline.jsearch_is_due(store, datetime(2026, 11, 9, 15, 30, tzinfo=tz)))
        self.assertTrue(pipeline.jsearch_is_due(store, datetime(2026, 11, 9, 16, 0, tzinfo=tz)))
        store.set_meta("jsearch_last_fetch", datetime(2026, 11, 9, 16, 0, tzinfo=tz).isoformat())
        self.assertFalse(pipeline.jsearch_is_due(store, datetime(2026, 11, 9, 20, 30, tzinfo=tz)))
        self.assertTrue(pipeline.jsearch_is_due(store, datetime(2026, 11, 10, 8, 0, tzinfo=tz)))

    def test_alert_urls_must_exist_in_the_email(self):
        message = {"from": "LinkedIn <jobalerts-noreply@linkedin.com>", "text": "GTM [https://www.linkedin.com/comm/jobs/view/123/?x=1]"}
        extracted = [
            {"title": "GTM", "company": "Acme", "location": "Berlin", "url": "https://www.linkedin.com/comm/jobs/view/123/?x=1"},
            {"title": "Invented", "company": "Bad", "location": "Berlin", "url": "https://evil.test/1"},
        ]
        jobs = gmail_alerts.jobs_from_alert(message, extracted)
        self.assertEqual([job.job_id for job in jobs], ["linkedin:123"])
        self.assertEqual(jobs[0].url, "https://www.linkedin.com/jobs/view/123")

    def test_unknown_sender_is_ignored(self):
        self.assertEqual(gmail_alerts.jobs_from_alert({"from": "x@notlinkedin.com", "text": ""}, []), [])
        self.assertIsNone(gmail_alerts.source_from_sender("x@fakelinkedin.com"))

    def test_processed_alert_is_not_read_twice(self):
        store = Store(":memory:")
        gmail = mock.Mock()
        gmail.search.return_value = ["m1"]
        gmail.read.return_value = {"from": "a@linkedin.com", "text": ""}
        ai = mock.Mock()
        ai.extract_alert_jobs.return_value = []
        gmail_alerts.fetch_alert_jobs(gmail, ai, store)
        gmail_alerts.fetch_alert_jobs(gmail, ai, store)
        self.assertEqual(gmail.read.call_count, 1)

    def test_gmail_message_prefers_html_with_links(self):
        encode = lambda text: base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        payload = {"parts": [
            {"mimeType": "text/plain", "body": {"data": encode("plain")}},
            {"mimeType": "text/html", "body": {"data": encode('<a href="https://x.test/1">Job</a>')}},
        ]}
        self.assertIn("[https://x.test/1]", message_text(payload))


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def run_pipeline(self, jobs, ai=None, gmail=None):
        self.ai = ai or FakeAI()
        self.gmail = gmail or FakeGmail()
        return pipeline.run_once({"test": lambda: jobs}, self.store, self.ai, self.gmail, "role")

    def test_match_is_emailed_once_with_a_link_to_the_app(self):
        self.run_pipeline([make_job()])
        self.assertEqual(self.store.get("arbeitnow:1")["status"], MATCHED)
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertIn(APP_URL, self.gmail.sent[0][2])

        gmail = self.gmail
        self.run_pipeline([make_job()], gmail=gmail)
        self.assertEqual(len(gmail.sent), 1)

    def test_irrelevant_title_is_not_stored_or_sent_to_ai(self):
        self.run_pipeline([make_job(title="Steuerberater")])
        self.assertEqual(self.ai.assessed, [])
        self.assertIsNone(self.store.get("arbeitnow:1"))

    def test_ai_rejection_goes_to_filtered_tab_without_email(self):
        self.run_pipeline([make_job()], ai=FakeAI(assessment(worth_applying=False, summary="Not a fit.")))
        job = self.store.get("arbeitnow:1")
        self.assertEqual((job["status"], job["filter_reason"]), (FILTERED_OUT, "Not a fit."))
        self.assertEqual([job["job_id"] for job in self.store.filtered_out(10)], ["arbeitnow:1"])
        self.assertEqual(self.gmail.sent, [])

    def test_hard_limit_overrides_a_positive_ai_decision(self):
        self.run_pipeline([make_job()], ai=FakeAI(assessment(on_site_days_per_week=5)))
        self.assertEqual(self.store.get("arbeitnow:1")["status"], FILTERED_OUT)

    def test_ai_confirms_a_near_duplicate(self):
        self.run_pipeline([make_job("1", title="GTM Engineer AI Automation")])
        ai = FakeAI(same=True)
        self.run_pipeline([make_job("2", source="berlinstartupjobs", title="GTM Engineer - AI & Automation", url="https://b.test/2")], ai=ai)
        self.assertEqual(self.store.get("berlinstartupjobs:2")["status"], DUPLICATE)
        self.assertEqual(ai.assessed, [])
        self.assertEqual(json.loads(self.store.get("arbeitnow:1")["other_links"]), ["https://b.test/2"])

    def test_similar_roles_stay_separate_when_ai_says_different(self):
        self.run_pipeline([make_job("1", title="GTM Engineer AI Automation")])
        self.run_pipeline([make_job("2", title="Senior GTM Engineer AI Automation", url="https://b.test/2")], ai=FakeAI(same=False))
        self.assertEqual(self.store.get("arbeitnow:2")["status"], MATCHED)

    def test_shortened_company_name_is_compared(self):
        self.run_pipeline([make_job("1", title="Go-to-Market Engineer", company="Northwind stock & people GmbH")])
        ai = FakeAI(same=True)
        self.run_pipeline([make_job("2", source="linkedin", title="GTM Engineer Go-to-Market", company="NORTHWIND", url="https://b.test/2")], ai=ai)
        self.assertEqual(self.store.get("linkedin:2")["status"], DUPLICATE)

    def test_recruiter_copy_with_another_company_is_compared(self):
        text = " ".join(f"word{index}" for index in range(80))
        self.run_pipeline([make_job("1", title="Revenue Operations Engineer", company="Acme", description=text)])
        ai = FakeAI(same=True)
        copy = make_job("2", source="berlinstartupjobs", title="AI Automation Engineer", company="Hays Recruiting", description=text, url="https://b.test/2")
        self.run_pipeline([copy], ai=ai)
        self.assertEqual(self.store.get("berlinstartupjobs:2")["status"], DUPLICATE)

    def test_reordered_title_scores_highly(self):
        self.assertGreaterEqual(pipeline.title_score("automation engineer ai", "ai automation engineer"), 0.99)

    def test_ai_is_not_asked_about_different_companies(self):
        self.run_pipeline([make_job("1")])
        ai = FakeAI()
        self.run_pipeline([make_job("2", company="Totally Different", url="https://b.test/2")], ai=ai)
        self.assertEqual(ai.compared, [])

    def test_ai_failure_keeps_job_queued_for_next_run(self):
        self.run_pipeline([make_job()], ai=FakeAI(error=AIError("down")))
        self.assertEqual(self.store.get("arbeitnow:1")["status"], NEW)
        self.assertEqual(self.gmail.sent, [])
        self.run_pipeline([make_job()])
        self.assertEqual(self.store.get("arbeitnow:1")["status"], MATCHED)

    def test_email_failure_retries_next_run(self):
        with self.assertRaises(RuntimeError):
            self.run_pipeline([make_job()], gmail=FakeGmail(fail=True))
        self.assertIsNone(self.store.get("arbeitnow:1")["notified_at"])
        self.run_pipeline([])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_broken_source_does_not_stop_others(self):
        def broken():
            raise RuntimeError("down")
        sources = {"broken": broken, "ok": lambda: [make_job()]}
        summary = pipeline.run_once(sources, self.store, FakeAI(), FakeGmail(), "role")
        self.assertEqual(summary.source_errors, ["broken"])
        self.assertEqual(self.store.get("arbeitnow:1")["status"], MATCHED)

    def test_repeated_source_failure_warns_once(self):
        def broken():
            raise RuntimeError("down")
        gmail = FakeGmail()
        for _ in range(8):
            pipeline.run_once({"broken": broken}, self.store, FakeAI(), gmail, "role")
        self.assertEqual(len([mail for mail in gmail.sent if "failed" in mail[0]]), 1)

    def test_arbeitnow_fetch_is_throttled(self):
        now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(pipeline.arbeitnow_is_due(self.store, now))
        self.store.set_meta("arbeitnow_last_fetch", now.isoformat())
        self.assertFalse(pipeline.arbeitnow_is_due(self.store, now + timedelta(minutes=30)))
        self.assertTrue(pipeline.arbeitnow_is_due(self.store, now + timedelta(minutes=60)))


class CareersFeedTest(unittest.TestCase):
    feed = careers.Feed("Acme", "ashby", "acme")

    def recent(self):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def response(self, payload, status=200):
        return mock.Mock(status_code=status, **{"json.return_value": payload})

    def test_greenhouse_uses_first_published_not_last_edit(self):
        job = careers.parse_greenhouse({
            "id": 7, "title": "GTM Engineer", "absolute_url": "https://x.test/7", "location": {"name": "Berlin"},
            "offices": [{"name": "Berlin", "location": "Berlin, Germany"}], "content": "&lt;p&gt;Hi&lt;/p&gt;",
            "first_published": "2026-09-17T02:55:42-04:00", "updated_at": "2026-11-06T08:45:50-05:00",
        }, self.feed)
        self.assertEqual((job.job_id, job.description, job.posted_at), ("greenhouse:acme:7", "Hi", "2026-09-17T02:55:42-04:00"))
        self.assertIn("Berlin, Germany", job.location)

    def test_ashby_includes_secondary_locations(self):
        job = careers.parse_ashby({
            "id": "a", "title": "T", "jobUrl": "https://x.test/a", "location": "Hamburg",
            "secondaryLocations": [{"location": "Berlin"}], "descriptionPlain": "Text", "publishedAt": "2026-10-12T20:01:14.094+00:00",
        }, self.feed)
        self.assertEqual(job.location, "Hamburg; Berlin")
        self.assertTrue(filters.is_in_search_area(job))
        self.assertTrue(filters.is_recent(job, datetime(2026, 10, 13, tzinfo=timezone.utc)))

    def test_remote_job_abroad_is_rejected_but_remote_in_europe_is_kept(self):
        def ashby(location, country):
            return careers.parse_ashby({
                "id": "a", "title": "T", "jobUrl": "https://x.test", "location": location, "isRemote": True,
                "address": {"postalAddress": {"addressCountry": country}},
            }, self.feed)
        self.assertFalse(filters.is_in_search_area(ashby("Remote", "United States")))
        self.assertFalse(filters.is_in_search_area(ashby("Remote - US", "")))
        self.assertTrue(filters.is_in_search_area(ashby("Remote", "Germany")))
        self.assertTrue(filters.is_in_search_area(ashby("Remote - EMEA", "")))
        self.assertTrue(filters.is_in_search_area(ashby("Remote", "")))

    def test_lever_date_and_lists(self):
        job = careers.parse_lever({
            "id": "l", "text": "T", "hostedUrl": "https://x.test/l", "categories": {"location": "Berlin"}, "country": "DE",
            "descriptionPlain": "Intro", "lists": [{"text": "You do", "content": "<li>Things</li>"}], "additionalPlain": "End",
            "createdAt": 1794205859000, "workplaceType": "hybrid",
        }, self.feed)
        self.assertEqual(job.posted_at, "2026-11-09T06:30:59+00:00")
        self.assertEqual(job.description, "Intro\nYou do\nThings\nEnd")

    def test_recruitee_date_is_readable(self):
        job = careers.parse_recruitee({
            "id": 1, "title": "T", "careers_url": "https://x.test", "location": "Berlin, Germany", "city": "Berlin",
            "published_at": "2026-10-28 08:34:19 UTC", "description": "<p>A</p>", "requirements": "<p>B</p>",
        }, self.feed)
        self.assertEqual(job.description, "A\nB")
        self.assertTrue(filters.is_recent(job, datetime(2026, 10, 29, tzinfo=timezone.utc)))

    def test_personio_xml(self):
        xml = b"""<workzag-jobs><position><id>9</id><office>Berlin</office><name>GTM Engineer</name>
        <jobDescriptions><jobDescription><name>Role</name><value><![CDATA[<p>Build</p>]]></value></jobDescription></jobDescriptions>
        <createdAt>2026-10-14T13:38:21+00:00</createdAt></position></workzag-jobs>"""
        job = careers.parse_personio(xml, careers.Feed("Acme", "personio", "acme"))[0]
        self.assertEqual((job.job_id, job.url, job.description), ("personio:acme:9", "https://acme.jobs.personio.de/job/9", "Role\nBuild"))

    def test_smartrecruiters_fetches_description_only_for_jobs_passing_rules(self):
        place = {"city": "Berlin", "fullLocation": "Berlin, Germany", "country": "de"}
        listing = {"totalFound": 2, "content": [
            {"id": "1", "name": "GTM Engineer", "location": place, "releasedDate": self.recent()},
            {"id": "2", "name": "Accountant", "location": place, "releasedDate": self.recent()},
        ]}
        detail = {"postingUrl": "https://x.test/1", "jobAd": {"sections": {"jobDescription": {"text": "<p>Build</p>"}}}}
        session = mock.Mock(**{"get.side_effect": [self.response(listing), self.response(detail)]})
        feed = careers.Feed("Acme", "smartrecruiters", "acme")
        with mock.patch("jobbot.sources.careers.time.sleep"):
            jobs = careers.fetch_careers(session, [feed])
        self.assertEqual([(job.title, job.description, job.url) for job in jobs], [("GTM Engineer", "Build", "https://x.test/1")])
        self.assertEqual(session.get.call_count, 2)

    def posting(self, title="GTM Engineer"):
        return {"id": "a", "title": title, "jobUrl": "https://x.test", "location": "Berlin", "publishedAt": self.recent()}

    def test_broken_feed_does_not_stop_the_others(self):
        good = self.response({"jobs": [self.posting()]})
        broken = mock.Mock(status_code=404, **{"raise_for_status.side_effect": requests.HTTPError("404")})
        session = mock.Mock(**{"get.side_effect": [broken, good]})
        with mock.patch("jobbot.sources.careers.time.sleep"):
            jobs = careers.fetch_careers(session, [self.feed, careers.Feed("Beta", "ashby", "beta")])
        self.assertEqual([job.company for job in jobs], ["Beta"])

    def test_rate_limited_system_is_left_alone_for_the_rest_of_the_run(self):
        limited = mock.Mock(status_code=429)
        good = self.response({"offers": [{"id": 1, "title": "GTM Engineer", "careers_url": "https://x.test", "location": "Berlin", "published_at": self.recent()}]})
        session = mock.Mock(**{"get.side_effect": [limited, good]})
        feeds = [self.feed, careers.Feed("Beta", "ashby", "beta"), careers.Feed("Gamma", "recruitee", "gamma")]
        with mock.patch("jobbot.sources.careers.time.sleep"):
            jobs = careers.fetch_careers(session, feeds)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual([job.company for job in jobs], ["Gamma"])

    def test_every_feed_failing_is_reported_as_a_source_failure(self):
        session = mock.Mock(**{"get.side_effect": requests.ConnectionError("down")})
        with mock.patch("jobbot.sources.careers.time.sleep"), self.assertRaises(RuntimeError):
            careers.fetch_careers(session, [self.feed])

    def test_load_feeds_skips_companies_without_a_supported_feed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "companies.csv"
            path.write_text("company,ats,slug\nAcme,ashby,acme\nNoFeed,,\nOther,workable,other\n", encoding="utf-8")
            self.assertEqual(careers.load_feeds(path), [careers.Feed("Acme", "ashby", "acme")])
            self.assertEqual(careers.load_feeds(Path(folder) / "missing.csv"), [])

    def test_backfill_keeps_older_jobs(self):
        posting = dict(self.posting(), publishedAt=(datetime.now(timezone.utc) - timedelta(hours=60)).isoformat())
        with mock.patch("jobbot.sources.careers.time.sleep"):
            normal = careers.fetch_careers(mock.Mock(**{"get.return_value": self.response({"jobs": [posting]})}), [self.feed])
            backfill = careers.fetch_careers(mock.Mock(**{"get.return_value": self.response({"jobs": [posting]})}), [self.feed], 72)
        self.assertEqual((len(normal), len(backfill)), (0, 1))

    def test_careers_fetch_is_throttled(self):
        store = Store(":memory:")
        now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(pipeline.careers_is_due(store, now))
        store.set_meta("careers_last_fetch", now.isoformat())
        self.assertFalse(pipeline.careers_is_due(store, now + timedelta(minutes=30)))
        self.assertTrue(pipeline.careers_is_due(store, now + timedelta(minutes=60)))


class NotificationTest(unittest.TestCase):
    def job(self, **overrides):
        values = {
            "job_id": "x:1", "title": "GTM", "company": "Acme", "location": "Berlin", "source": "x", "fit_score": 80,
            "url": "https://x.test/1", "reasons": '["Good."]', "gaps": "[]", "unknowns": "[]", "other_links": "[]",
            "posted_at": "2026-11-08T09:00:00+00:00",
        }
        values.update(overrides)
        return values

    def test_card_shows_only_score_title_location_source_and_date(self):
        now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
        _, text, html = notify.build_digest([self.job(source="ashby", reasons='["Long AI reasoning."]')], None, [], now)
        for expected in ("80", "GTM", "Acme", "Berlin", "Company site", "2 days ago (8 Nov)"):
            self.assertIn(expected, html)
            self.assertIn(expected, text)
        self.assertNotIn("Long AI reasoning", html + text)

    def test_posted_label(self):
        now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(notify.posted_label("2026-11-10T07:00:00Z", now), "Today (10 Nov)")
        self.assertEqual(notify.posted_label("2026-11-09T23:30:00+00:00", now), "Today (10 Nov)")
        self.assertEqual(notify.posted_label("2026-11-09T10:00:00+00:00", now), "Yesterday (9 Nov)")
        self.assertEqual(notify.posted_label("", now), "")

    def test_posting_text_is_escaped(self):
        _, _, html = notify.build_digest([self.job(title="<img src=x onerror=alert(1)>")], None, [])
        self.assertNotIn("<img", html)

    def test_non_https_link_is_not_clickable(self):
        _, _, html = notify.build_digest([self.job(url="javascript:alert(1)")], None, [])
        self.assertNotIn("href=\"javascript", html)

    def test_subject_counts_jobs(self):
        self.assertEqual(notify.build_digest([self.job()], None, [])[0], "[Job Bot] 1 new job to review")


class WebAppTest(unittest.TestCase):
    """Runs the real server on a free localhost port, against a temporary database."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "jobs.db"
        store = Store(self.database)
        store.add(make_job(title="GTM Engineer <script>alert(1)</script>"))
        store.save_assessment("arbeitnow:1", assessment(), None)
        store.add(make_job(native_id="2", title="Revenue Operations Engineer", company="Beta"))
        store.save_assessment("arbeitnow:2", assessment(), "Requires German above a basic level")
        handler = self.handler = type("TestHandler", (web.Handler,), {"database": self.database})
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        handler.allowed_origins = {self.origin}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def get(self, path="/"):
        return requests.get(self.origin + path, timeout=5)

    def post(self, path, data, origin=None):
        return requests.post(self.origin + path, data=data, headers={"Origin": origin or self.origin}, allow_redirects=False, timeout=5)

    def test_review_tab_lists_matches_with_text_escaped(self):
        page = self.get()
        self.assertEqual(page.status_code, 200)
        self.assertIn("GTM Engineer &lt;script&gt;", page.text)
        self.assertNotIn("<script>alert", page.text)
        self.assertNotIn("Revenue Operations Engineer", page.text)
        self.assertIn("default-src 'none'", page.headers["Content-Security-Policy"])

    def test_filtered_tab_shows_the_reason(self):
        self.assertIn("Requires German above a basic level", self.get("/?view=filtered").text)

    def test_marking_applied_moves_the_job_to_the_applied_tab(self):
        response = self.post("/status", {"job_id": "arbeitnow:1", "status": "applied", "view": "review"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/?view=review#job-arbeitnow-1")
        self.assertNotIn("GTM Engineer", self.get().text)
        self.assertIn("GTM Engineer", self.get("/?view=active").text)
        self.assertEqual(Store(self.database).get("arbeitnow:1")["application_status"], "applied")

    def test_each_status_is_a_one_click_button(self):
        page = self.get().text
        for label in ("Applied", "Interviewing", "Rejected", "Not interested"):
            self.assertIn(f'value="{label.lower()}">{label}</button>', page)
        self.assertNotIn("Back to review", page)
        self.assertNotIn("<select", page)
        self.post("/status", {"job_id": "arbeitnow:1", "status": "applied", "view": "review"})
        self.assertIn("Back to review", self.get("/?view=active").text)

    def test_notes_are_saved(self):
        response = self.post("/notes", {"job_id": "arbeitnow:1", "notes": "Sent CV", "view": "review"})
        self.assertEqual(response.headers["Location"], "/?view=review#job-arbeitnow-1-detail")
        self.assertIn("Sent CV", self.get().text)

    def test_pasting_a_link_adds_the_job_and_opens_it(self):
        self.handler.make_ai = staticmethod(lambda: FakePostingAI())
        self.handler.make_session = staticmethod(lambda: mock.Mock(**{"get.return_value": page_response(JOB_PAGE)}))
        with mock.patch("jobbot.sources.manual._is_public", return_value=True):
            response = self.post("/add", {"url": "https://jobs.example.com/gtm", "text": ""})
        self.assertEqual(response.status_code, 303)
        self.assertRegex(response.headers["Location"], r"^/\?view=review&notice=added#job-manual-[0-9a-f]+-detail$")
        page = self.get("/?view=review&notice=added").text
        self.assertIn("Job added.", page)
        self.assertIn("EUR 65,000-80,000 per year", page)
        self.assertIn("Build automations for sales.", page)

    def test_closed_site_link_without_text_shows_a_message(self):
        self.handler.make_ai = staticmethod(lambda: FakePostingAI())
        response = self.post("/add", {"url": "https://www.linkedin.com/jobs/view/1", "text": ""})
        self.assertEqual(response.headers["Location"], "/?view=review&notice=closed_site&url=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F1")
        page = self.get(response.headers["Location"]).text
        self.assertIn("Paste the job text", page)
        self.assertIn('value="https://www.linkedin.com/jobs/view/1"', page)
        self.assertIn("<details open>", page)
        self.assertIn('value="&quot;&gt;&lt;script&gt;"', self.get("/?url=%22%3E%3Cscript%3E").text)

    def test_notice_codes_never_echo_request_text(self):
        self.assertNotIn("<b>hi</b>", self.get("/?notice=<b>hi</b>").text)

    def test_posting_text_can_be_saved_for_a_title_only_job(self):
        store = Store(self.database)
        store.set_description("arbeitnow:1", "")
        self.assertIn("This source gave only the title", self.get().text)
        self.post("/description", {"job_id": "arbeitnow:1", "text": "Full posting text", "view": "review"})
        self.assertEqual(Store(self.database).get("arbeitnow:1")["description"], "Full posting text")

    def test_post_from_another_site_is_rejected(self):
        response = self.post("/status", {"job_id": "arbeitnow:1", "status": "applied"}, origin="https://evil.test")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Store(self.database).get("arbeitnow:1")["application_status"], "to review")

    def test_unknown_status_and_filtered_job_cannot_be_set(self):
        self.assertEqual(self.post("/status", {"job_id": "arbeitnow:1", "status": "hacked"}).status_code, 400)
        self.assertEqual(self.post("/status", {"job_id": "arbeitnow:2", "status": "applied"}).status_code, 404)


JOB_PAGE = """<html><head><script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting",
"title": "GTM Engineer", "hiringOrganization": {"@type": "Organization", "name": "Acme"},
"jobLocation": {"@type": "Place", "address": {"addressLocality": "Berlin", "addressCountry": "DE"}},
"datePosted": "2026-11-08", "validThrough": "2026-12-31T00:00:00", "employmentType": "FULL_TIME",
"baseSalary": {"@type": "MonetaryAmount", "currency": "EUR", "value": {"minValue": 65000, "maxValue": 80000, "unitText": "YEAR"}},
"description": "&lt;p&gt;Build automations for sales.&lt;/p&gt;"}</script></head><body>Ignore this</body></html>"""


class FakePostingAI(FakeAI):
    def __init__(self, fields=None, **kwargs):
        super().__init__(**kwargs)
        self.fields = fields or {"is_job_posting": True, "title": "RevOps Engineer", "company": "Beta", "location": "Berlin", "remote": False, "posted_date": ""}
        self.extracted = []

    def extract_posting(self, text):
        self.extracted.append(text)
        return self.fields


def page_response(body="", status=200, location=None):
    response = mock.MagicMock(status_code=status, encoding="utf-8", is_redirect=location is not None, headers={"Location": location or ""})
    response.__enter__.return_value = response
    response.raw.read.return_value = body.encode()
    return response


class ManualJobTest(unittest.TestCase):
    def setUp(self):
        public_ip = mock.patch("jobbot.sources.manual.socket.getaddrinfo", return_value=[(None, None, None, "", ("93.184.216.34", 443))])
        public_ip.start()
        self.addCleanup(public_ip.stop)

    def test_structured_posting_is_read_without_ai(self):
        ai = FakePostingAI()
        session = mock.Mock(**{"get.return_value": page_response(JOB_PAGE)})
        job = manual.build_job("https://jobs.example.com/1", "", session, ai)
        self.assertEqual((job.title, job.company, job.location, job.posted_at), ("GTM Engineer", "Acme", "Berlin, DE", "2026-11-08"))
        self.assertEqual(job.description, "Build automations for sales.")
        self.assertEqual(job.details, {"Employment type": "Full Time", "Salary": "EUR 65,000-80,000 per year", "Closing date": "2026-12-31"})
        self.assertEqual(ai.extracted, [])

    def test_page_without_structured_data_is_read_by_ai(self):
        ai = FakePostingAI()
        session = mock.Mock(**{"get.return_value": page_response("<p>We hire a RevOps Engineer</p>")})
        job = manual.build_job("https://jobs.example.com/2", "", session, ai)
        self.assertEqual((job.title, job.company), ("RevOps Engineer", "Beta"))
        self.assertEqual(ai.extracted, ["We hire a RevOps Engineer"])

    def test_minimal_structured_data_falls_back_to_page_text(self):
        page = """<script type="application/ld+json">{"@type": "JobPosting", "title": "AI Enablement Specialist (m/f/d) FP&A",
        "datePosted": "2026-11-09"}</script><h1>AI Enablement Specialist</h1><p>Company: Siemens Energy</p>"""
        ai = FakePostingAI(fields={"is_job_posting": True, "title": "Other", "company": "Siemens Energy", "location": "Berlin", "remote": False, "posted_date": ""})
        job = manual.build_job("https://jobs.example.com/3", "", mock.Mock(**{"get.return_value": page_response(page)}), ai)
        self.assertEqual((job.title, job.company, job.location, job.posted_at), ("AI Enablement Specialist (m/f/d) FP&A", "Siemens Energy", "Berlin", "2026-11-09"))
        self.assertIn("Company: Siemens Energy", job.description)

    def test_closed_sites_are_never_fetched(self):
        session = mock.Mock()
        with self.assertRaises(manual.ManualJobError) as caught:
            manual.build_job("https://www.linkedin.com/jobs/view/1", "", session, FakePostingAI())
        self.assertEqual(caught.exception.code, "closed_site")
        session.get.assert_not_called()

    def test_closed_site_works_with_pasted_text(self):
        session = mock.Mock()
        job = manual.build_job("https://de.linkedin.com/jobs/view/1", "RevOps Engineer at Beta in Berlin", session, FakePostingAI())
        self.assertEqual((job.title, job.description), ("RevOps Engineer", "RevOps Engineer at Beta in Berlin"))
        session.get.assert_not_called()

    def test_private_addresses_are_refused(self):
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
            with mock.patch("jobbot.sources.manual.socket.getaddrinfo", return_value=[(None, None, None, "", (address, 443))]):
                with self.assertRaises(manual.ManualJobError) as caught:
                    manual.fetch_page(mock.Mock(), "https://looks-public.example.com/")
                self.assertEqual(caught.exception.code, "private_address")

    def test_redirect_to_a_closed_site_is_refused(self):
        session = mock.Mock(**{"get.return_value": page_response(location="https://www.linkedin.com/jobs/view/1")})
        with self.assertRaises(manual.ManualJobError) as caught:
            manual.fetch_page(session, "https://short.example.com/x")
        self.assertEqual((caught.exception.code, session.get.call_count), ("closed_site", 1))

    def test_http_and_non_links_are_refused(self):
        for url in ("http://jobs.example.com/1", "file:///etc/passwd", "not a link"):
            with self.assertRaises(manual.ManualJobError):
                manual.check_url(url)

    def test_page_that_is_not_a_job(self):
        ai = FakePostingAI(fields={"is_job_posting": False, "title": "", "company": "", "location": "", "remote": False, "posted_date": ""})
        session = mock.Mock(**{"get.return_value": page_response("<p>Cookie settings</p>")})
        with self.assertRaises(manual.ManualJobError) as caught:
            manual.build_job("https://example.com/", "", session, ai)
        self.assertEqual(caught.exception.code, "not_a_job")


class TrackManualJobTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_new_job_is_assessed_and_put_in_review_without_an_email(self):
        job_id, already = pipeline.track_manual_job(make_job(source="manual"), self.store, FakeAI(), "role")
        row = self.store.get(job_id)
        self.assertFalse(already)
        self.assertEqual((row["status"], row["application_status"], row["fit_score"], row["filter_reason"]), (MATCHED, "to review", 80, None))
        self.assertEqual(self.store.unnotified_matches(), [])

    def test_ai_concern_is_kept_as_a_warning(self):
        ai = FakeAI(assessment(language_requirement="required"))
        job_id, _ = pipeline.track_manual_job(make_job(source="manual"), self.store, ai, "role")
        self.assertEqual((self.store.get(job_id)["status"], self.store.get(job_id)["filter_reason"]), (MATCHED, "Requires German above a basic level"))

    def test_same_link_already_tracked_is_reused(self):
        self.store.add(make_job())
        self.store.save_assessment("arbeitnow:1", assessment(), None)
        job_id, already = pipeline.track_manual_job(make_job(source="manual", url="https://example.com/arbeitnow/1"), self.store, FakeAI(), "role")
        self.assertEqual((job_id, already), ("arbeitnow:1", True))

    def test_filtered_out_job_moves_to_review_with_its_reason(self):
        self.store.add(make_job())
        self.store.save_assessment("arbeitnow:1", assessment(), "Requires German above a basic level")
        ai = FakeAI()
        job_id, already = pipeline.track_manual_job(make_job(source="manual", native_id="x"), self.store, ai, "role")
        row = self.store.get(job_id)
        self.assertEqual((job_id, already, row["status"], row["filter_reason"]), ("arbeitnow:1", False, MATCHED, "Requires German above a basic level"))
        self.assertEqual(ai.assessed, [])

    def test_ai_failure_still_tracks_the_job(self):
        job_id, _ = pipeline.track_manual_job(make_job(source="manual"), self.store, FakeAI(error=AIError("down")), "role")
        row = self.store.get(job_id)
        self.assertEqual((row["status"], row["fit_score"]), (MATCHED, None))


class StoreMigrationTest(unittest.TestCase):
    def test_existing_database_gets_the_new_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            old = sqlite3.connect(path)
            old.executescript(store_module.SCHEMA)
            old.execute("INSERT INTO jobs (job_id, dedupe_key, source, title, company, location, url, first_seen_at, status) VALUES ('a:1', 'k', 'a', 'T', 'C', '', '', '2026-11-01', 'matched')")
            old.commit()
            old.close()
            self.assertEqual(Store(path).get("a:1")["application_status"], "to review")


class OpenAIParsingTest(unittest.TestCase):
    def test_reads_structured_output(self):
        response = {"status": "completed", "output": [
            {"type": "reasoning"},
            {"type": "message", "content": [{"type": "output_text", "text": '{"same_role": true}'}]},
        ]}
        self.assertEqual(OpenAIClient._parse(response), {"same_role": True})

    def test_incomplete_and_refusal_raise(self):
        with self.assertRaises(AIError):
            OpenAIClient._parse({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}})
        with self.assertRaises(AIError):
            OpenAIClient._parse({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]})


class SchedulingTest(unittest.TestCase):
    def test_active_hours(self):
        self.assertTrue(within_active_hours(datetime(2026, 11, 9, 7, 0)))
        self.assertTrue(within_active_hours(datetime(2026, 11, 9, 20, 59)))
        self.assertFalse(within_active_hours(datetime(2026, 11, 9, 21, 0)))
        self.assertFalse(within_active_hours(datetime(2026, 11, 9, 6, 59)))


if __name__ == "__main__":
    unittest.main()
