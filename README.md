# Slack bridge

Channels and Slack as one conversation: link a channel on your Cloudmorrow
server to a channel in Slack, and what is written in either shows up in the
other, replies in their threads.

A [Quill](https://github.com/Cloudmorrow/cloudmorrow/blob/main/docs/QUILLS.md)
for Cloudmorrow, an add-on to [Channels](https://github.com/Cloudmorrow/quill-channels):
install it from the Quill Catalog (Channels comes with it), or from this
repository with `cm quill add --source https://github.com/Cloudmorrow/quill-slack --ref v1.0.0`.
It needs Cloudmorrow 0.94.0 or later, the first that lets a Quill deliver
into a datamodel it uses (`[[delivers]]`).

## How it works

- **Your own Slack app** carries the messages: made in your workspace from
  the manifest the settings page shows, in two minutes. There is no service
  in between; the server talks to slack.com and Slack talks to the server.
- **Written here, posted in Slack**: a message in a linked channel is posted
  in the Slack channel by the app, under the writer's name. A reply goes into
  its thread.
- **Said in Slack, brought here**: Slack's Events API tells the server, and
  the message arrives in the channel as from its sender there — *Ada (Slack)*
  — never as from anybody on this server. It is news like any other message:
  unread, pushed, counted. Every event is checked against the app's Signing
  Secret as well as the webhook's own token.
- **Not carried**: edits, deletions, reactions, files (a file says *shared a
  file in Slack*), and anything said before the link. Direct messages are
  never linked.

## Setting it up

An administrator, under **Administration › Quills › Slack bridge**:

1. **Make the app.** At api.slack.com/apps, *Create New App* › *From a
   manifest*, pick the workspace, paste the JSON the page shows, and
   *Install to Workspace*.
2. **Connect it.** Paste the app's *Bot User OAuth Token* (`xoxb-…`) and its
   *Signing Secret*, and press *Connect Slack*.
3. **Let Slack tell the server.** In the app's *Event Subscriptions*, switch
   it on, paste the Quill's **events** webhook address (with its token, as
   the Quill's sheet shows it) as the *Request URL*, subscribe to the bot
   events `message.channels` and `message.groups`, and save. The server must
   be reachable from the internet at that address.
4. **Link channels.** *Link to Slack* beside a channel, and the Slack
   channel's name. A public Slack channel is joined by the app; a private one
   needs `/invite @Cloudmorrow` in it first.

The people who post into a linked channel need the Slack bridge's data in
one of their circles (the install sheet asks which), so their messages can
find the link and go across.

## What it adds to your Cloudmorrow

| | |
| --- | --- |
| Needs | Channels, installed with it |
| Datamodels | uses `channel`, `message`; extends `message` with `slack.ts`; introduces `slack.workspace`, `slack.link`, `slack.member` |
| Brings in from outside | `message`: what is said in a linked Slack channel, as from its sender there |
| Screens | Slack, the settings, for an administrator on the Quill's sheet |
| Actions | Connect Slack; Link to Slack; Unlink; Disconnect Slack — an administrator's |
| Code | `quill.py`, in the sandbox: a hook when a message is written, a webhook for Slack's events; reaches slack.com only |

## Working on it

```
uv sync                  # .venv with Cloudmorrow and pytest
cm quill check           # the manifest, as a server would install it
cm quill test            # tests/, against the real record store and gate, Slack faked
cm quill test --sandbox  # the same, with quill.py in the sandbox
```
