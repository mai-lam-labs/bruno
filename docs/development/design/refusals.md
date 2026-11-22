---
description: "How Bruno refuses: the code and data a client acts on, and a message that says what failed, written for a model to act on"
---

# Refusals

**The code and `data` are the contract; the message is guidance.** A client tells refusals
apart by their code and `data` ([SPEC.md](../../SPEC.md#interface)), and its tests do the
same. The message is free text, so it can change without changing the interface, which is why
it can carry more than the code does.

**A message says what failed.** The reader is as often a model as a person, acting on the
message in its next step. Where Bruno's own rule decides what can succeed, the message names it:
sign again for the amount required, name a later block, pay in another token, try again in a few
seconds, or, where the client can do nothing, that it's the operator's to fix. It doesn't guess
at causes outside Bruno: guidance like that fits one case and misleads the next, and every
message carrying it is one more to keep true.

**A failed simulation decodes its revert.** Revert data is the answer a client most needs and
can least read, so the message decodes the errors of what Bruno implements: `Error(string)`,
`Panic(uint256)`, the runner's own errors, and the EntryPoint's `FailedOp` and
`FailedOpWithRevert`, the revert inside it decoded as any is. Anything else is named by its
selector, the revert data whole in the error's `revertData`. It says what the revert was, not
why.

**One catalogue, the same text on both relays.** A message is part of what a client sees, and
the guidance in it is what drifts first: reworded on one relay, or left behind when a behaviour
changes. So every refusal's text is a template in one catalogue, written once in Python and
exported to the Worker as the x402 description is, and neither relay writes one inline. A test
checks that every template is used by both and that the Worker's copy is current; the revert
decoding is in the golden vectors. A behaviour that changes has one place to check its words
against. Type and shape errors keep their decoders' own wording: msgspec's on Python, fixed
strings on the Worker.
