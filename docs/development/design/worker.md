---
description: "The relay on Cloudflare Workers: one Durable Object a chain, its alarms as the clock, idle costing nothing, and the treasury on a cron"
---

# Cloudflare Workers

The relay ported to TypeScript (`worker/`). The Worker routes each request to its chain's
Durable Object, one a chain, which keeps the queue, the batch and the funding in flight in its
SQLite storage. Its alarms are the clock ([clock and reads](reads.md#clock-and-reads)): each step an alarm.

**Idle costs nothing.** An object with nothing queued holds no alarm, so it's evicted from
memory and billed nothing; a deployment serving a quiet chain costs only its requests.

**The treasury runs in the chain's object,** as the Python treasury holds its relay: an hourly
cron trigger calls each chain with a treasury, and its object runs it when its interval is up,
keeping the snapshot in its storage. A cron rather than an alarm: nothing has to wake the
object after a deploy for the treasury to start.
