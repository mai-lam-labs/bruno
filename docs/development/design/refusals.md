---
description: "How Bruno refuses: the code and data a client acts on, and a message that says what failed and what to do next, written for a model to act on"
---

# Refusals

**The code and `data` are the contract; the message is guidance.** A client tells refusals
apart by their code and `data` ([SPEC.md](../../SPEC.md#interface)), and its tests do the
same. The message is free text, so it can change without changing the interface, which is why
it can carry more than the code does.

**A message says what failed, then what to do next.** The reader is as often a model as a
person, acting on the message in its next step: a model given a refusal that only describes
the failure explains it correctly and gives up. So each message names the next step that can
succeed: sign again for the amount required, name a later block, pay in another token, try
again in a few seconds, or, where the client can do nothing, that it's the operator's to fix.

**A failed simulation decodes its revert.** Revert data is the answer a client most needs and
can least read, so the message decodes what it can: `Error(string)`, `Panic(uint256)`, the
runner's own errors and the EntryPoint's `FailedOp`; anything else is named by its selector.
For a call's own revert it adds the cause that's easy to miss, that the call is made by a
one-off contract, not the payer's account.

**One catalogue, the same text on both relays.** A message is part of what a client sees, and
the guidance in it is what drifts first: reworded on one relay, or left behind when a behaviour
changes. So every refusal's text is a template in one catalogue, written once in Python and
exported to the Worker as the x402 description is, and neither relay writes one inline. A test
checks that every template is used by both and that the Worker's copy is current; the revert
decoding is in the golden vectors. A behaviour that changes has one place to check its words
against. Type and shape errors keep their decoders' own wording: msgspec's on Python, fixed
strings on the Worker.
