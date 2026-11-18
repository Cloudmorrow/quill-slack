"""Slack bridge: a channel here and a channel in Slack, kept as one conversation.

Channels draws the conversation; this only carries messages across.

- **Out**, the `to_slack` hook: a message written in a linked channel is
  posted to the linked Slack channel by the workspace's app, under the name
  of whoever wrote it, in its thread when it is a reply. It runs as that
  person, and remembers Slack's id for the message on it (`slack.ts`).
- **In**, the `events` webhook: Slack's Events API tells of a message in a
  linked Slack channel, and it is delivered into the channel here as from
  its sender there — `Ada (Slack)` — never as from anybody on this server.
  The core checks the webhook's token; this checks Slack's signature too.

The workspace (the app's bot token and signing secret, the Quill's alone),
the links and the names of the people in Slack are records of the Quill's
own. Nothing runs beside the server, and nothing leaves it but the messages
of linked channels, to slack.com.
"""

import datetime as dt
import hashlib
import hmac
import html
import json
import re
import urllib.parse

from cloudmorrow.quill import (
    Invalid,
    NotFound,
    Refused,
    action,
    error,
    hook,
    redraw,
    respond,
    toast,
    ui,
    view,
    webhook,
)

WORKSPACE, LINK, MEMBER = "slack.workspace", "slack.link", "slack.member"
CHANNEL, MESSAGE = "channel", "message"
TS = "slack.ts"
API = "https://slack.com/api/"
# How old a signed request from Slack may be before it is taken for a replay.
FRESH_SECONDS = 300
# What a message from Slack says after its sender's name.
FROM_SLACK = " (Slack)"
SETTINGS_ARE = "Administration › Quills › Slack bridge"
# Kinds of message event that are somebody saying something; edits, deletions,
# joins and bots are not carried across.
SAID = (None, "", "thread_broadcast", "file_share", "me_message")

# The app, as Slack makes one from a manifest: its bot and what it may do.
APP_MANIFEST = {
    "display_information": {
        "name": "Cloudmorrow",
        "description": "Keeps channels on our Cloudmorrow server and in Slack as one conversation.",
    },
    "features": {"bot_user": {"display_name": "Cloudmorrow", "always_online": True}},
    "oauth_config": {
        "scopes": {
            "bot": [
                "channels:history",
                "channels:join",
                "channels:read",
                "groups:history",
                "groups:read",
                "chat:write",
                "chat:write.customize",
                "users:read",
            ]
        }
    },
    "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False},
}
BOT_EVENTS = ("message.channels", "message.groups")


class SlackError(Exception):
    """What Slack said went wrong, in words."""


# -- Slack's Web API -----------------------------------------------------------------------
# Slack's own words for what went wrong, said so an administrator knows what to do.
SAID_ERRORS = {
    "invalid_auth": "Slack did not take the token",
    "not_authed": "Slack did not take the token",
    "token_revoked": "the token was revoked in Slack; connect again",
    "account_inactive": "the app was removed from the workspace; connect again",
    "not_in_channel": "the app is not in that Slack channel; /invite @Cloudmorrow in it",
    "channel_not_found": "Slack has no such channel the app can see",
    "is_archived": "that Slack channel is archived",
    "ratelimited": "Slack asked to slow down; it is tried again with the next message",
}


def call(ctx, token, method, **args):
    """One call to Slack's Web API, as a form: its answer, or a SlackError."""
    form = urllib.parse.urlencode(
        {k: v for k, v in args.items() if v not in (None, "")}
    )
    try:
        response = ctx.fetch(
            API + method,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=form,
        )
    except Refused:
        raise SlackError("the bridge may not reach slack.com") from None
    except (Invalid, OSError, ValueError) as exc:
        raise SlackError(f"slack.com could not be reached: {exc}") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data.get("ok"):
        code = str((data or {}).get("error") or f"Slack answered {response.status}")
        if code == "missing_scope":
            raise SlackError(
                f"the app lacks the permission {data.get('needed') or '?'}: add it under "
                "OAuth & Permissions in Slack, reinstall the app, and connect again"
            )
        raise SlackError(SAID_ERRORS.get(code, code))
    return data


# -- the records ---------------------------------------------------------------------------
def workspace(ctx):
    """The one workspace, with its secrets (a listing has them as null), or None."""
    found = ctx.records.list(WORKSPACE)
    return ctx.records.get(WORKSPACE, found[0].id) if found else None


def note(ctx, ws, status, **more):
    """What the bridge last did, on the workspace — when whoever it runs as may say so."""
    try:
        ctx.records.patch(WORKSPACE, ws.id, {"status": status, **more})
    except (Refused, NotFound, Invalid):
        pass


def administrators_only(ctx):
    return (
        None
        if ctx.user.admin
        else error("The Slack bridge is an administrator's to set up")
    )


def channel_named(ctx, said):
    """A channel here, by its id or its name (with or without #)."""
    said = str(said or "").strip()
    channels = [c for c in ctx.records.list(CHANNEL) if c.get("kind") != "direct"]
    for found in channels:
        if found.id == said:
            return found
    bare = said.lstrip("#").strip().lower()
    for found in channels:
        if str(found.get("name") or "").strip().lower() == bare:
            return found
    return None


def links_of(ctx):
    return ctx.records.list(LINK)


# -- the settings, for an administrator ---------------------------------------------------
def setup_steps():
    manifest = json.dumps(APP_MANIFEST, indent=2)
    return ui.stack(
        ui.text(
            "A Slack app of your own carries the messages: made in your workspace, from the manifest "
            "below, in two minutes. Nothing runs anywhere but on this server and in Slack.",
            style="muted",
        ),
        ui.text("1. Make the app", style="subtitle"),
        ui.markdown(
            "At **api.slack.com/apps** press *Create New App* › *From a manifest*, pick the workspace, "
            "choose JSON, and paste this:\n\n```json\n" + manifest + "\n```\n\n"
            "Then *Install to Workspace* and allow it."
        ),
        ui.text("2. Connect it here", style="subtitle"),
        ui.markdown(
            "Two things from the app's pages in Slack: the **Bot User OAuth Token** (OAuth & Permissions, "
            "starts with `xoxb-`) and the **Signing Secret** (Basic Information › App Credentials)."
        ),
        ui.button("Connect Slack", action="connect", tone="primary"),
    )


def events_steps():
    return ui.markdown(
        "**So Slack tells this server what is said:** in the app's settings in Slack, open *Event "
        "Subscriptions*, switch it on, and paste as the *Request URL* this Quill's **events** webhook "
        "address, with its token, as this sheet shows it under the Quill's webhooks. Slack checks it at "
        "once. Under *Subscribe to bot events* add `"
        + "` and `".join(BOT_EVENTS)
        + "`, then *Save Changes*. The server must be reachable from the internet at that address."
    )


@view("settings")
def settings(ctx):
    """The bridge: drawn on the Quill's sheet under Administration › Quills, for an administrator."""
    if not ctx.user.admin:
        return ui.empty("The Slack bridge is an administrator's to set up.")
    ws = workspace(ctx)
    if ws is None:
        return setup_steps()
    links = links_of(ctx)
    linked = {link["channel"]: link for link in links}
    rows = []
    for channel in sorted(
        (c for c in ctx.records.list(CHANNEL) if c.get("kind") != "direct"),
        key=lambda c: str(c.get("name") or "").lower(),
    ):
        name = f"#{channel.get('name') or '?'}"
        link = linked.get(channel.id)
        if link is not None:
            rows.append(
                ui.row(
                    ui.text(
                        f"{name}  ↔  {link.get('slack_name') or link.get('slack_channel')} in Slack"
                    ),
                    ui.button("Unlink", action="unlink", record=link, tone="danger"),
                )
            )
        else:
            rows.append(
                ui.row(
                    ui.text(name, style="muted"),
                    ui.button(
                        "Link to Slack", action="link", args={"channel": channel.id}
                    ),
                )
            )
    heard = str(ws.get("heard_at") or "")
    return ui.stack(
        ui.row(
            ui.stat("Workspace", ws.get("team") or "?"),
            ui.stat("Linked channels", len(links)),
            ui.stat("Brought in from Slack", ws.get("received") or 0),
        ),
        ui.text(str(ws.get("status") or ""), style="muted"),
        ui.text(
            f"Last heard from Slack: {heard[:16].replace('T', ' ')}"
            if heard
            else "Not heard from Slack yet.",
            style="muted",
        ),
        events_steps(),
        ui.text("Channels", style="subtitle"),
        ui.text(
            "A linked channel and its Slack channel are one conversation: what is written in either "
            "shows up in the other, replies in their thread. Messages from before the link stay where they are.",
            style="muted",
        ),
        *(rows or [ui.empty("There are no channels here yet; make one in Channels.")]),
        ui.divider(),
        ui.button("Disconnect Slack", action="disconnect", tone="danger"),
    )


@action("connect")
def connect(ctx, token, signing_secret):
    if (refused := administrators_only(ctx)) is not None:
        return refused
    token = str(token).strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    signing_secret = str(signing_secret).strip()
    if not token.startswith("xoxb-"):
        return error(
            "That is not a bot token: the Bot User OAuth Token starts with xoxb-, on the app's "
            "OAuth & Permissions page in Slack."
        )
    if not re.fullmatch(r"[0-9a-f]{20,64}", signing_secret):
        return error(
            "That is not a Signing Secret: it is letters a–f and digits, under Basic Information › App Credentials."
        )
    try:
        who = call(ctx, token, "auth.test")
    except SlackError as exc:
        return error(f"Connecting failed: {exc}.")
    fields = {
        "token": token,
        "signing_secret": signing_secret,
        "team": str(who.get("team") or ""),
        "team_id": str(who.get("team_id") or ""),
        "url": str(who.get("url") or ""),
        "bot_user": str(who.get("user_id") or ""),
        "bot_id": str(who.get("bot_id") or ""),
        "status": f"Connected to {who.get('team') or 'the workspace'}",
    }
    ws = workspace(ctx)
    if ws is None:
        ctx.records.create(WORKSPACE, fields, scope="public")
    else:
        ctx.records.patch(WORKSPACE, ws.id, fields)
    return redraw()


def slack_channels(ctx, token):
    """Every channel the app can see in Slack, public and private, not archived."""
    found, cursor = [], ""
    for _ in range(10):
        page = call(
            ctx,
            token,
            "conversations.list",
            types="public_channel,private_channel",
            exclude_archived="true",
            limit="1000",
            cursor=cursor,
        )
        found += page.get("channels") or []
        cursor = str((page.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            break
    return found


@action("link")
def link(ctx, channel, slack):
    if (refused := administrators_only(ctx)) is not None:
        return refused
    ws = workspace(ctx)
    if ws is None:
        return error("Connect Slack first.")
    here = channel_named(ctx, channel)
    if here is None:
        return error(f"There is no channel {channel} here.")
    wanted = str(slack).strip().lstrip("#").strip()
    if not wanted:
        return error("Which Slack channel? Its name, like #general.")
    token = str(ws.get("token") or "")
    try:
        there = next(
            (
                c
                for c in slack_channels(ctx, token)
                if c.get("id") == wanted
                or str(c.get("name") or "").lower() == wanted.lower()
            ),
            None,
        )
    except SlackError as exc:
        return error(f"Slack would not list its channels: {exc}.")
    if there is None:
        return error(
            f"The app sees no channel #{wanted} in Slack. A private one needs the app invited first: "
            "/invite @Cloudmorrow in it."
        )
    for other in links_of(ctx):
        if other["channel"] == here.id:
            return error(
                f"#{here.get('name')} is linked to {other.get('slack_name')} already."
            )
        if other["slack_channel"] == there["id"]:
            return error(
                f"#{there.get('name')} in Slack is linked to another channel already."
            )
    if not there.get("is_member"):
        if there.get("is_private"):
            return error(
                f"Invite the app into #{there.get('name')} in Slack first: /invite @Cloudmorrow in it."
            )
        try:
            call(ctx, token, "conversations.join", channel=there["id"])
        except SlackError as exc:
            return error(f"The app could not join #{there.get('name')}: {exc}.")
    ctx.records.create(
        LINK,
        {
            "channel": here.id,
            "slack_channel": there["id"],
            "slack_name": f"#{there.get('name')}",
        },
        scope="public",
    )
    return toast(
        f"#{here.get('name')} and #{there.get('name')} in Slack are one conversation now"
    )


@action("unlink")
def unlink(ctx, link):
    if (refused := administrators_only(ctx)) is not None:
        return refused
    ctx.records.delete(LINK, link.id)
    return toast(f"{link.get('slack_name')} is no longer linked")


@action("disconnect")
def disconnect(ctx):
    if (refused := administrators_only(ctx)) is not None:
        return refused
    for model in (LINK, MEMBER, WORKSPACE):
        for record in ctx.records.list(model):
            ctx.records.delete(model, record.id)
    return redraw()


# -- out: a message written here, posted in Slack --------------------------------------------
@hook
def to_slack(ctx, change):
    """Runs as whoever wrote the message. Not one that came from Slack, not one somebody
    else is said to have written, and only in a linked channel."""
    message = change.record
    if message.get(TS) or message.owner != ctx.user.username:
        return
    links = ctx.records.list(LINK, channel=message.get("channel"))
    if not links:
        return
    ws = workspace(ctx)
    if ws is None:
        return
    thread = ""
    if message.get("reply_to"):
        try:
            thread = str(ctx.records.get(MESSAGE, message["reply_to"]).get(TS) or "")
        except (NotFound, Refused):
            thread = ""
    try:
        posted = call(
            ctx,
            str(ws.get("token") or ""),
            "chat.postMessage",
            channel=links[0]["slack_channel"],
            text=str(message.get("body") or ""),
            username=ctx.user.name or ctx.user.username,
            thread_ts=thread,
        )
    except SlackError as exc:
        print(f"to Slack: {links[0].get('slack_name')}: {exc}")
        note(ctx, ws, f"Could not post to {links[0].get('slack_name')}: {exc}")
        return
    ctx.records.patch(MESSAGE, message.id, {TS: str(posted.get("ts") or "")})


# -- in: a message said in Slack, brought here --------------------------------------------
def signed_by_slack(request, secret, now):
    """Slack's signature on the request: v0, the timestamp and the body, under the Signing Secret."""
    stamp = request.headers.get("x-slack-request-timestamp", "")
    said = request.headers.get("x-slack-signature", "")
    if not secret or not stamp.isdigit() or not said:
        return False
    if abs(now - int(stamp)) > FRESH_SECONDS:
        return False
    base = b"v0:" + stamp.encode() + b":" + request.body
    wanted = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(wanted, said)


def member_name(ctx, token, user_id):
    """Somebody in Slack by name: kept once asked, asked of Slack the first time."""
    if not user_id:
        return "Somebody"
    for found in ctx.records.list(MEMBER, user_id=user_id):
        return str(found.get("name") or user_id)
    try:
        person = call(ctx, token, "users.info", user=user_id).get("user") or {}
    except SlackError as exc:
        print(f"from Slack: no name for {user_id}: {exc}")
        return user_id
    profile = person.get("profile") or {}
    name = (
        str(profile.get("display_name") or "").strip()
        or str(profile.get("real_name") or "").strip()
        or str(person.get("real_name") or "").strip()
        or str(person.get("name") or "").strip()
        or user_id
    )
    ctx.records.create(MEMBER, {"user_id": user_id, "name": name}, scope="public")
    return name


LINK_RE = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")


def plain_text(ctx, token, text):
    """Slack's message markup, as words: <@U1> is @Ada, <#C1|general> is #general,
    <https://x|here> is here (https://x), and &amp; &lt; &gt; are themselves."""

    def one(match):
        target, label = match.group(1), match.group(2)
        if target.startswith("@"):
            return "@" + (label or member_name(ctx, token, target[1:]))
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!"):
            word = target[1:].split("^")[0]
            return "@" + (label or word).lstrip("@")
        if label and label != target:
            return f"{label} ({target})"
        return target.removeprefix("mailto:")

    return html.unescape(LINK_RE.sub(one, str(text or "")))


@webhook
def events(ctx, request):
    """Slack's Events API. The core checked the webhook's token; Slack's signature is checked
    here. Every answer is quick and 200 unless the request is not Slack's, so Slack does not
    send the same event again."""
    try:
        body = request.json()
    except ValueError:
        return respond(json={"ok": False, "error": "not JSON"}, status=400)
    if not isinstance(body, dict):
        return respond(json={"ok": False, "error": "not an event"}, status=400)
    ws = workspace(ctx)
    secret = str(ws.get("signing_secret") or "") if ws else ""
    now = int(ctx.now().timestamp())
    if body.get("type") == "url_verification":
        # Slack checks the address before anything is connected, too: the
        # webhook's token has said it is ours, and an echo tells nothing.
        if secret and not signed_by_slack(request, secret, now):
            return respond(
                json={"ok": False, "error": "not signed by Slack"}, status=401
            )
        return respond(json={"challenge": str(body.get("challenge") or "")})
    if ws is None:
        return {"ok": True, "ignored": "not connected"}
    if not signed_by_slack(request, secret, now):
        return respond(json={"ok": False, "error": "not signed by Slack"}, status=401)
    if ws.get("team_id") and body.get("team_id") and body["team_id"] != ws["team_id"]:
        return {"ok": True, "ignored": "another workspace"}
    event = body.get("event") or {}
    if body.get("type") != "event_callback" or event.get("type") != "message":
        return {"ok": True, "ignored": "not a message"}
    if (
        event.get("subtype") not in SAID
        or event.get("bot_id")
        or event.get("user") == ws.get("bot_user")
    ):
        return {"ok": True, "ignored": "not somebody saying something"}
    links = ctx.records.list(LINK, slack_channel=str(event.get("channel") or ""))
    if not links:
        return {"ok": True, "ignored": "not a linked channel"}
    channel = links[0]["channel"]
    ts = str(event.get("ts") or "")
    if not ts or ctx.records.list(MESSAGE, **{"channel": channel, TS: ts}):
        return {"ok": True, "ignored": "here already"}
    token = str(ws.get("token") or "")
    name = member_name(ctx, token, str(event.get("user") or ""))
    text = plain_text(ctx, token, event.get("text")).strip()
    if not text and event.get("files"):
        text = "(shared a file in Slack)"
    if not text:
        return {"ok": True, "ignored": "nothing said"}
    fields = {"channel": channel, "body": text, TS: ts}
    thread = str(event.get("thread_ts") or "")
    if thread and thread != ts:
        parent = ctx.records.list(MESSAGE, **{"channel": channel, TS: thread})
        if parent:
            fields["reply_to"] = parent[0].id
    made = ctx.records.create(
        MESSAGE, fields, owner=ctx.user.username, by=name + FROM_SLACK
    )
    note(
        ctx,
        ws,
        f"Brought in a message from {name} in {links[0].get('slack_name')}",
        received=int(ws.get("received") or 0) + 1,
        heard_at=dt.datetime.fromtimestamp(now, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return {"ok": True, "id": made.id}
