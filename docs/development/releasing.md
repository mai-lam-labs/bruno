---
description: "How a release goes from its branch to a deployed relay: the version set, the merge, the tag, the deploy and the verify run, one step at a time"
---

# Releasing

A release goes from a branch to a deployed relay in the steps below, in this order, one at a
time. Each step that changes something outside your working copy (a push to main, a tag, a
deploy, a live run) is taken on its own, and the next waits until it has finished and been
checked. Releases are immutable on GitHub: once a release is published, its tag and assets are
final, so a mistake found after that is fixed in a new version, never by moving a tag.

## On the branch

Every change is made on the release's branch, `next-X.Y.Z`, through its pull request to main.
Nothing is committed to main directly.

1. **Temporary notes go.** A working note kept on the branch (`WORK-X.Y.Z.md`) is deleted
   before merging. What it leaves for the next release is carried into the next branch, after
   the merge, never into this one.
2. **The relay's version.** Set `version` in `pyproject.toml`, then run `make lock install`, so
   the lock and the installed package both carry it, then `make vectors`, which writes the
   version into the documents the Worker serves (`worker/src/openapi.json`, `openrpc.json`, and
   `front.json`'s source link). Run before `make install`, `make vectors` writes the old
   version back.
3. **The packages' versions.** `@mailam/bruno` and `@mailam/bruno-agentkit` move only if
   `clients/` or `integrations/` changed since the last release, each in its own `deno.json`.
   The integration moves whenever the client does, changed itself or not: it's published
   pinned to the client's version, so left behind it would bring the old client with it. The
   tag's publish jobs skip a version that's already on JSR or npm.
4. **The diff is what the release should contain.** Read `git diff --stat main` for anything
   temporary or unintended.
5. **Rehearse the verify run** on the local chain with `make anvil-verify`. It spends nothing,
   and it's run here because nothing after the tag can be changed: what the deployed relay will
   be verified by is tried before the merge.
6. **Run the live check on Sepolia** with `make check-sepolia`. It runs the branch's own two
   relays (the Python relay, and the Worker under workerd) against Sepolia itself, through its
   real nodes and its builder, from the test wallet (`.secrets/sepolia/wallet.key`). Nothing
   deployed is involved, so it can run before the merge, and it's the only run against real
   nodes and a real builder before the tag. It spends Sepolia ETH and takes a few minutes.
7. **The pull request's checks pass**, on its last commit.

## Merging and tagging

8. **Merge.** A fast-forward keeps the branch's commit hash on main: `git push origin
   next-X.Y.Z:main`, once the pull request's runs on that commit have finished. GitHub marks
   the pull request merged when its head reaches main.
9. **Main's checks pass.** The push to main runs `check` and publishes the documentation site.
   Wait for both.
10. **Tag.** Tag main's commit `vX.Y.Z`, matching `pyproject.toml`'s version, and push the tag.
11. **The release run passes.** The tag runs the checks again, then pushes the image, publishes
    the packages and creates the GitHub release. Wait for all of it.

## Deploying

12. **Deploy the Worker** with `make worker-deploy`, from the tagged commit with nothing
    uncommitted: the Worker is built from the working copy. One upload replaces its code and
    its configuration together, and keeps its secrets. The configuration is
    `config.default.json` with your `config.local.json` merged over it, built again if either
    changed, checked by `make preflight` first, and nothing is deployed if a chain isn't ready
    ([Deployment](../DEPLOYMENT.md)).
13. **Verify the deployed relay** with `make sepolia-verify`. It spends Sepolia ETH and takes a
    few minutes. `make ethereum-verify` spends real money and is run only when it's needed.
14. **Check that it answers.** bruno.mailam.app's root page gives the new version and links its
    source at the tag.
