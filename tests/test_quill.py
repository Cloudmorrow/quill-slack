"""Slack bridge's tests: the real record store and gate, and quill.py, with Slack's API faked.

`cm quill test` runs them; `cm quill test --sandbox` runs the same tests
with quill.py in the sandbox, as a server runs it.
"""

import hashlib
import hmac
import json
import time
import urllib.parse

import pytest
from cloudmorrow.quill.testing import Harness

API = "https://slack.com/api/"
TOKEN = "xoxb-1-2-abc"
SECRET = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def q():
    with Harness(".") as harness:
        yield harness


def slack_answers(q, method, body):
    q.fetch.add(API + method, method="POST", json={"ok": True, **body})


def sent_to(q, method):
    """The forms posted to one Slack method, in order."""
    return [
        dict(urllib.parse.parse_qsl(s["body"].decode()))
        for s in q.fetch.sent
        if s["url"] == API + method
    ]


def connected(q):
    slack_answers(
        q,
        "auth.test",
        {
            "team": "Bramlabs",
            "team_id": "T1",
            "url": "https://bramlabs.slack.com/",
            "user_id": "UBOT",
            "bot_id": "BBOT",
        },
    )
    done = q.act("connect", token=TOKEN, signing_secret=SECRET)
    assert done.ok and not done.error, done.error


def public_channel(q, name):
    """A public channel, as Channels makes one: everybody on the server is in it."""
    made = q.state.records.create(
        q._principal(), "channel", {"name": name, "kind": "public"}, scope="public"
    )
    return q.get("channel", made.id)


def linked(q, name="general", slack_name="general", slack_id="C1"):
    channel = public_channel(q, name)
    slack_answers(
        q,
        "conversations.list",
        {
            "channels": [
                {
                    "id": slack_id,
                    "name": slack_name,
                    "is_member": True,
                    "is_private": False,
                }
            ]
        },
    )
    done = q.act("link", channel=channel.id, slack=f"#{slack_name}")
    assert done.ok and not done.error, done.error
    return channel


def signed(body, *, secret=SECRET, at=None):
    raw = json.dumps(body).encode()
    stamp = str(int(at if at is not None else time.time()))
    signature = (
        "v0="
        + hmac.new(
            secret.encode(), b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256
        ).hexdigest()
    )
    return raw, {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": signature}


def event(q, **fields):
    body = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "message", **fields},
    }
    raw, headers = signed(body)
    return q.webhook("events", body=raw, headers=headers)


# -- setting it up ---------------------------------------------------------------------------
def test_before_connecting_the_settings_say_how_to_make_the_app(q):
    text = q.view("settings").text()
    assert "From a manifest" in text and "chat:write.customize" in text
    assert "Connect Slack" in q.view("settings").buttons


def test_connecting_proves_the_token_and_keeps_both_secrets_from_everybody(q):
    connected(q)
    [ws] = q.list("slack.workspace")
    assert ws["team"] == "Bramlabs" and ws["bot_user"] == "UBOT"
    # Secret to the Quill's code alone: null through the record API.
    assert ws.get("token") is None and ws.get("signing_secret") is None
    assert "Bramlabs" in q.view("settings").text()


def test_a_token_that_is_not_a_bot_token_is_refused_before_slack_is_asked(q):
    assert "xoxb-" in q.act("connect", token="xoxp-person", signing_secret=SECRET).error
    assert q.fetch.sent == []


def test_a_token_slack_refuses_is_said_in_words(q):
    q.fetch.add(
        API + "auth.test", method="POST", json={"ok": False, "error": "invalid_auth"}
    )
    assert (
        "Slack did not take the token"
        in q.act("connect", token=TOKEN, signing_secret=SECRET).error
    )
    assert q.list("slack.workspace") == []


def test_linking_joins_a_public_channel_the_app_is_not_in(q):
    connected(q)
    channel = q.seed("channel", {"name": "general", "kind": "public"})
    slack_answers(
        q,
        "conversations.list",
        {
            "channels": [
                {"id": "C9", "name": "random", "is_member": False, "is_private": False}
            ]
        },
    )
    slack_answers(q, "conversations.join", {})
    done = q.act("link", channel="#general", slack="random")
    assert done.ok and "one conversation" in done.toast
    assert sent_to(q, "conversations.join") == [{"channel": "C9"}]
    [link] = q.list("slack.link")
    assert (link["channel"], link["slack_channel"], link["slack_name"]) == (
        channel.id,
        "C9",
        "#random",
    )


def test_a_private_slack_channel_needs_the_app_invited_first(q):
    connected(q)
    q.seed("channel", {"name": "board", "kind": "public"})
    slack_answers(
        q,
        "conversations.list",
        {
            "channels": [
                {"id": "G1", "name": "board", "is_member": False, "is_private": True}
            ]
        },
    )
    assert (
        "/invite @Cloudmorrow" in q.act("link", channel="board", slack="#board").error
    )
    assert q.list("slack.link") == []


def test_a_channel_is_linked_once(q):
    connected(q)
    linked(q)
    assert (
        "linked to #general already"
        in q.act("link", channel="general", slack="#general").error
    )


def test_linking_and_disconnecting_are_an_administrators(q):
    connected(q)
    channel = q.seed("channel", {"name": "general", "kind": "public"})
    sam = q.as_user("sam")
    assert sam.act("link", channel=channel.id, slack="#general").refused
    with pytest.raises(AssertionError, match="for an administrator"):
        sam.view("settings")


def test_disconnecting_forgets_the_workspace_and_every_link(q):
    connected(q)
    linked(q)
    assert q.act("disconnect").ok
    assert q.list("slack.workspace") == [] and q.list("slack.link") == []


# -- out: written here, posted in Slack --------------------------------------------------------
def test_a_message_in_a_linked_channel_is_posted_in_slack_under_the_writers_name(q):
    connected(q)
    channel = linked(q)
    slack_answers(q, "chat.postMessage", {"ts": "1700000000.000100"})
    message = q.seed("message", {"channel": channel.id, "body": "Lunch at noon?"})
    [posted] = sent_to(q, "chat.postMessage")
    assert posted == {"channel": "C1", "text": "Lunch at noon?", "username": "alice"}
    assert q.get("message", message.id)["slack.ts"] == "1700000000.000100"


def test_a_reply_here_goes_into_its_thread_in_slack(q):
    connected(q)
    channel = linked(q)
    slack_answers(q, "chat.postMessage", {"ts": "1700000000.000100"})
    parent = q.seed("message", {"channel": channel.id, "body": "Lunch?"})
    q.seed("message", {"channel": channel.id, "body": "Yes", "reply_to": parent.id})
    assert sent_to(q, "chat.postMessage")[1]["thread_ts"] == "1700000000.000100"


def test_a_message_in_a_channel_nobody_linked_stays_here(q):
    connected(q)
    linked(q)
    other = q.seed("channel", {"name": "private-talk", "kind": "public"})
    q.seed("message", {"channel": other.id, "body": "Just us"})
    assert sent_to(q, "chat.postMessage") == []


def test_when_slack_will_not_take_a_message_the_settings_say_why(q):
    connected(q)
    channel = linked(q)
    q.fetch.add(
        API + "chat.postMessage",
        method="POST",
        json={"ok": False, "error": "not_in_channel"},
    )
    q.seed("message", {"channel": channel.id, "body": "Hello?"})
    assert "/invite @Cloudmorrow" in q.view("settings").text()


# -- in: said in Slack, brought here ---------------------------------------------------------
def test_slack_checking_the_address_is_answered_with_its_challenge(q):
    answer = q.webhook(
        "events", json={"type": "url_verification", "challenge": "abc123"}
    )
    assert answer["status"] == 200 and json.loads(answer["body"]) == {
        "challenge": "abc123"
    }


def test_a_message_said_in_slack_arrives_as_from_its_sender_there(q):
    connected(q)
    channel = linked(q)
    slack_answers(
        q,
        "users.info",
        {
            "user": {
                "id": "U1",
                "profile": {"display_name": "Ada", "real_name": "Ada L"},
            }
        },
    )
    answer = event(
        q,
        channel="C1",
        user="U1",
        text="Hi <@U1>, see <https://x.org|this> &amp; that",
        ts="1700000001.000200",
    )
    assert answer["status"] == 200, answer
    [message] = q.list("message")
    assert message["channel"] == channel.id
    assert message["body"] == "Hi @Ada, see this (https://x.org) & that"
    assert message.owner == "Ada (Slack)"
    assert message["slack.ts"] == "1700000001.000200"
    # Not posted back to Slack, and the name is asked once.
    assert sent_to(q, "chat.postMessage") == []
    event(q, channel="C1", user="U1", text="again", ts="1700000002.000200")
    assert len(sent_to(q, "users.info")) == 1
    assert q.get("slack.workspace", q.list("slack.workspace")[0].id)["received"] == 2


def test_a_reply_in_slack_lands_in_its_thread_here(q):
    connected(q)
    channel = linked(q)
    slack_answers(q, "chat.postMessage", {"ts": "1700000000.000100"})
    parent = q.seed("message", {"channel": channel.id, "body": "Lunch?"})
    slack_answers(
        q, "users.info", {"user": {"id": "U1", "profile": {"display_name": "Ada"}}}
    )
    event(
        q,
        channel="C1",
        user="U1",
        text="Yes!",
        ts="1700000005.000100",
        thread_ts="1700000000.000100",
    )
    reply = next(m for m in q.list("message") if m["body"] == "Yes!")
    assert reply["reply_to"] == parent.id


def test_the_same_event_twice_is_one_message(q):
    connected(q)
    linked(q)
    slack_answers(
        q, "users.info", {"user": {"id": "U1", "profile": {"display_name": "Ada"}}}
    )
    for _ in range(2):
        event(q, channel="C1", user="U1", text="Once", ts="1700000003.000100")
    assert len(q.list("message")) == 1


def test_what_is_not_somebody_saying_something_is_not_brought_in(q):
    connected(q)
    linked(q)
    event(q, channel="C1", user="UBOT", text="mine", ts="1.1")
    event(
        q,
        channel="C1",
        bot_id="BBOT",
        subtype="bot_message",
        text="posted by the bridge",
        ts="1.2",
    )
    event(q, channel="C1", user="U1", subtype="message_changed", ts="1.3")
    event(q, channel="C1", user="U1", subtype="channel_join", text="joined", ts="1.4")
    event(q, channel="C2", user="U1", text="in a channel nobody linked", ts="1.5")
    assert q.list("message") == []


def test_an_event_slack_did_not_sign_is_refused(q):
    connected(q)
    linked(q)
    body = {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "C1",
            "user": "U1",
            "text": "x",
            "ts": "1.1",
        },
    }
    raw, headers = signed(body, secret="f" * 32)
    assert q.webhook("events", body=raw, headers=headers)["status"] == 401
    raw, headers = signed(body, at=time.time() - 3600)
    assert q.webhook("events", body=raw, headers=headers)["status"] == 401
    assert q.webhook("events", json=body)["status"] == 401
    assert q.list("message") == []


def test_anybody_writing_in_a_linked_channel_is_posted_under_their_own_name(q):
    connected(q)
    channel = linked(q)
    slack_answers(q, "chat.postMessage", {"ts": "1700000009.000100"})
    sam = q.as_user("sam")
    message = sam.seed("message", {"channel": channel.id, "body": "From Sam"})
    assert sent_to(q, "chat.postMessage")[-1]["username"] == "sam"
    assert sam.get("message", message.id)["slack.ts"] == "1700000009.000100"
