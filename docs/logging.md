# Logging

`LOG_LEVEL` controls console / service journal output (default `INFO`).
`DM_LOG_TARGET` enables Discord DM alerts; `DM_LOG_LEVEL` controls their
threshold independently (default `WARNING`). DMs include a timestamp, severity,
logger name, and any traceback. Private Patreon RSS URLs are redacted in both
destinations, including debug messages and tracebacks.

At INFO, RSS fallback and manual polls produce one summary with the trigger,
status, new/modified entry counts, errors, unread entries, posting attempts,
actual posts, and elapsed time. Deduplicated attempts do not count as posts.
Startup, watcher milestones, episode posting, and failures remain visible.
Empty rapid polls during an expected release window are DEBUG only.

Set `LOG_LEVEL=DEBUG` when investigating polling: it includes individual update
results, before/after feed state, HTTP cache headers and Content-Type, lock
timings, and watcher retry timings. Routine feed-state database queries are
skipped unless DEBUG logging is enabled. Existing YouTube outage summaries and
console-only gateway/loop-lag diagnostics retain their alerting policy.

## The old "bozo feed" warning

`bozo` is [feedparser's term](https://feedparser.readthedocs.io/en/stable/bozo.html)
for a feed standards problem. `reader` continues parsing when the problem is a
missing Content-Type header; actual parsing/retrieval failures are reported
separately. This particular warning is now DEBUG and says
`RSS feed has no Content-Type header; parser continuing`. Other recoverable
feed warnings say `RSS feed metadata warning` and retain WARNING severity.

[reader 3.24 changed to structured logging](https://reader.readthedocs.io/en/latest/changelog.html#version-3-24).
The former DM-only filter depended on the old logger name and exception repr,
so upgrading reader could let the warning through again. The logging policy
handles both formats and changes severity at the emitting logger, before
console or DM thresholds are applied. It does not change feed parsing or
invent a response header.
