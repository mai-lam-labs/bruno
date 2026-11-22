---
description: "How Bruno refuses: the code and the reason a client acts on, and a message that says what failed, written for a model to act on"
---

# Refusals

**The code and `reason` are the contract; the message is guidance.** Every refusal has a name,
and every error the relay raises carries it as its `reason`
([SPEC.md](../../SPEC.md#interface)), typed in the published documents and in the client: a
client tells refusals apart by code and `reason`, never by matching the message, and the tests
do the same. Matching text is the pattern this rules out, because a message reworded breaks
every client that read it. An error carries its refusal, not text, and its message is rendered
from it, so the two can't disagree. Only what's raised is a refusal, so the list a client
branches on is exactly what it can be sent. The message is free text, so it can change without
changing the interface.

**A message says what failed.** The reader is as often a model as a person, acting on the
message in its next step. Where Bruno's own rule decides what can succeed, the message names it:
sign again for the amount required, name a later block, pay in another token, try again in a few
seconds, or, where the client can do nothing, that it's the operator's to fix. It doesn't guess
at causes outside Bruno: guidance like that fits one case and misleads the next, and every
message carrying it is one more to keep true.

**A failed simulation decodes its revert.** Revert data is the answer a client most needs and
can least read, so the standard errors of what Bruno implements (Solidity's, the EntryPoint's,
the runner's own) are decoded, a revert nested in one included, and anything else is named by
its selector, the revert data whole beside it. It says what the revert was, not why.

**One catalogue, the same text on both relays.** A message is part of what a client sees, and
the guidance in it is what drifts first: reworded on one relay, or left behind when a behaviour
changes. So every refusal's text is a template in one catalogue, written once in Python and
exported to the Worker as the x402 description is, and neither relay writes one inline. A test
checks that every template is used by both and that the Worker's copy is current; the revert
decoding is in the golden vectors. A behaviour that changes has one place to check its words
against. Type and shape errors keep their decoders' own wording: msgspec's on Python, fixed
strings on the Worker.
