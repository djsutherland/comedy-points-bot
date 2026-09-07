# YouTube WebSub deployment

The bot subscribes to the Blank Check channel using its immutable channel ID,
`UCI8t9VKTB6uD91NvlC15oJA`. WebSub is enabled when all three variables are
set:

```dotenv
YOUTUBE_WEBSUB_CALLBACK_URL=https://example.com/an-unguessable-youtube-websub-path
YOUTUBE_WEBSUB_SECRET=<a stable random secret under 200 bytes>
YOUTUBE_API_KEY=<server-restricted YouTube Data API v3 key>
```

The API key is required for posting: the bot waits for public status, title,
description, and a positive duration. Missing configuration disables WebSub,
leaving RSS active. Metadata failures stay queued for retry; they do not produce
an incomplete card that suppresses the richer RSS version.

The embedded callback server listens only on localhost by default:

```dotenv
YOUTUBE_WEBSUB_BIND_HOST=127.0.0.1
YOUTUBE_WEBSUB_BIND_PORT=8080
```

Route the exact public callback path to that listener with the VPS reverse
proxy. For example, a Caddy route can use:

```caddyfile
handle /an-unguessable-youtube-websub-path {
    reverse_proxy 127.0.0.1:8080
}
```

Generate the stable subscription secret with, for example,
`openssl rand -hex 32`. Keep the callback on HTTPS. The bot requests a
subscription at startup, verifies HMAC signatures on deliveries, records the
lease granted by the hub, and renews at 80% of the lease duration.

Subscription requests explicitly use `hub.verify=async` and a 60-second HTTP
timeout (metadata fetches still use 15 seconds). Response headers are logged
before the response body is read; failures identify which stage timed out.
After an uncertain request failure, the bot continues accepting verification
for up to 60 seconds. A verification already received is not discarded just
because the subscription POST subsequently times out. Failed attempts retry
after 15, 30, 60, 120, 240, then 300 seconds, resetting after success. A
`Retry-After` header from the hub extends that delay, up to 15 minutes. These
delays are in addition to the request/verification waits.

For debugging, the subscription log lines include the callback and topic URLs,
the hub's response headers, and its response body (truncated). Every request
that reaches the callback listener is logged with its path, query string,
forwarding headers, and response status, including requests the router
rejects. The subscription secret itself is never logged; only its length is.

A hub response of `503 Transient error; please try again later` with
`Retry-After: 120` after roughly 20 seconds is a hub-side failure: the hub
returns it before contacting the callback at all, for any callback, topic, or
mode (observed 2026-09-07). Nothing on our side fixes it; the retry loop picks
up the subscription once the hub recovers. A `400` names the invalid parameter
instead, and a missing verification (`awaiting verification` followed by
`verification timed out`) points at the reverse proxy or callback path.

Cross-source claims are stored in `episode-claims.sqlite` by default. Override
that path with `EPISODE_CLAIMS_DB` if deployment state lives elsewhere. Keep the
database across restarts. Completed claims expire after 30 days so genuinely
reused titles can be posted again. Both source IDs are retained for each match,
so editing the losing source's title does not create another announcement.

The same database holds a durable WebSub inbox. Valid deliveries are committed
before acknowledgment, and failed work retries with exponential backoff capped
at 15 minutes. Restarting resumes unfinished work. Completed delivery hashes are
kept for 30 days to absorb replays. Run only one bot instance against this state.

On the first configured WebSub startup, a persistent activation timestamp is
recorded. Videos published before that timestamp are ignored by WebSub, including
title/description updates to already-posted episodes. RSS still handles these
episodes normally. Restarting does not move the cutoff. Enable WebSub before the
next release; it intentionally does not backfill older videos.

Interrupted Discord sends are reconciled against this bot's episode cards in
channel history before retrying (after a 30-second settling interval). Grant the
bot **Read Message History**, as well as its existing channel/send permissions.
If history cannot be read, the pending claim remains retryable instead of being
silently treated as posted. This is recovery, not a transactional exactly-once
guarantee across Discord and SQLite; manually deleted/edited cards or unusually
late delivery of a timed-out send can defeat history matching. Role mentions
remain a best-effort separate send.

RSS polling cadence and stop-on-success behavior are unchanged: a YouTube win
can stop the rapid RSS watcher. This intentionally prioritizes delivery over
measuring the losing source's exact availability time.

Console and Discord log handlers redact private Patreon RSS URLs, including
third-party reader warnings and exception tracebacks. This does not sanitize
existing logs or rotate credentials that were previously exposed.
