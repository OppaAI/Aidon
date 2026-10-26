"""Old Aidon capabilities, rebuilt in new Aidon, with fake APIs.

No real Tavily, YouTube, Fish, Threads, Instagram, GitHub,
or email call is made here. All external calls use an
injected fake, and missing keys are errors, never invented
results. Publishing and email to non-owners are approval
gated, fail closed.
"""

from src import search as search_mod
from src import youtube as youtube_mod
from src import voice as voice_mod
from src import social as social_mod
from src import github_tools as github_mod
from src import email as email_mod
from src.scheduler_loop import (AutonomyTracker, NudgeTracker,
                                SchedulerLoop, daily_report)


def test_search_tavily_and_missing_key():
    calls = []

    def fake_post(url, payload, headers):
        calls.append((url, payload))
        return {"results": [{"title": "T", "url": "https://example.invalid", "content": "snippet"}]}

    result = search_mod.search("dinner", backend="tavily", tavily_key="k", post=fake_post)
    assert result["ok"] is True and result["results"][0]["title"] == "T"
    assert search_mod.search("dinner", backend="tavily", tavily_key="")["ok"] is False


def test_youtube_search_and_missing_key():
    def fake_get(url):
        return {"items": [{"id": {"videoId": "abc"}, "snippet": {"title": "Vid", "channelTitle": "Ch"}}]}

    result = youtube_mod.search_videos("roast beef", "k", get=fake_get)
    assert result["results"][0]["url"] == "https://www.youtube.com/watch?v=abc"
    assert youtube_mod.search_videos("x", "")["ok"] is False


def test_voice_tts_fish_and_transcribe_missing():
    result = voice_mod.tts("hello", "k", post=lambda url, key, payload: b"MP3")
    assert result["ok"] is True and result["audio"] == b"MP3"
    assert voice_mod.tts("hello", "")["ok"] is False
    assert voice_mod.transcribe(b"audio", "")["ok"] is False
    assert voice_mod.transcribe(b"audio", "k", post=lambda url, key, data: {"text": "hi"})["text"] == "hi"


def test_social_publishing_approval_gated():
    assert social_mod.publish_threads("hi", "acct", "tok", approve=lambda kind, text: False)["ok"] is False
    assert social_mod.publish_threads("hi", "acct", "tok", approve=None)["ok"] is False
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        return {"id": "post1"}

    result = social_mod.publish_threads("hi", "acct", "tok", approve=lambda kind, text: True, post=fake_post)
    assert result["ok"] is True and len(calls) == 2
    assert social_mod.publish_instagram("https://example.invalid/x.jpg", "cap", "ig", "tok",
                                        approve=lambda kind, text: True, post=fake_post)["ok"] is True


def test_github_search_and_fetch():
    def fake_get(url, token):
        if "search" in url:
            return {"items": [{"full_name": "a/b", "html_url": "https://github.com/a/b", "description": "d"}]}, {}
        return {"content": "aGVsbG8="}, {}

    # search path returns a dict, fetch path returns a dict with content
    result = github_mod.search_repos("aidon", get=lambda url, token: ({"items": [{"full_name": "a/b", "html_url": "https://github.com/a/b", "description": "d"}]}, {}))
    assert result["results"][0]["full_name"] == "a/b"
    fetched = github_mod.fetch_file("a", "b", "README.md", get=fake_get)
    assert fetched["text"] == "hello"


def test_scheduler_loop_daily_report_nudges_autonomy(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "learned").mkdir()
    (home / "learned" / "last_run.json").write_text('{"status": "done", "project_id": "dinner"}')
    report = daily_report(home)
    assert "done" in report and "dinner" in report

    sent = []

    tracker = NudgeTracker(nudge_minutes=15, channel="C1")
    assert tracker.check("card-1", 0, 100, slack_sender=lambda ch, text: sent.append((ch, text))) is False
    assert tracker.check("card-1", 0, 1000, slack_sender=lambda ch, text: sent.append((ch, text))) is True
    assert sent == [("C1", "Approval pending: card-1")]
    assert tracker.check("card-1", 0, 2000, slack_sender=lambda ch, text: sent.append((ch, text))) is False

    autonomy = AutonomyTracker(enabled=True, threshold=2, decay_days=14)
    assert autonomy.should_auto_approve("low", 100) is False
    autonomy.record_approval("low", 100)
    autonomy.record_approval("low", 101)
    assert autonomy.should_auto_approve("low", 102) is True
    assert autonomy.should_auto_approve("high", 102) is False
    autonomy.record_rejection("low")
    assert autonomy.should_auto_approve("low", 103) is False


def test_email_draft_owner_and_corrected(tmp_path):
    home = tmp_path / "home"
    draft = email_mod.make_draft(home, "other@example.invalid", "Statement", "v1")
    assert email_mod.send_draft(home, draft, owner_email="owner@example.invalid",
                                approve=lambda kind, text: False,
                                sender=lambda to, subj, body: {"ok": True})["ok"] is False
    sent = []
    draft2 = email_mod.make_draft(home, "owner@example.invalid", "Hi", "v1")
    result = email_mod.send_draft(home, draft2, owner_email="owner@example.invalid",
                                  sender=lambda to, subj, body: sent.append((to, subj)) or {"ok": True})
    assert result["ok"] is True
    draft3 = email_mod.make_draft(home, "owner@example.invalid", "Hi", "v2")
    assert draft3["subject"].startswith("Corrected:")
