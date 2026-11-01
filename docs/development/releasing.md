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
   The tag's publish jobs skip a version that's already on JSR or npm.
4. **The diff is what the release should contain.** Read `git diff --stat main` for anything
   temporary or unintended.
5. **The pull request's checks pass**, on its last commit.

## Merging and tagging

6. **Merge.** A fast-forward keeps the branch's commit hash on main: `git push origin
   next-X.Y.Z:main`, once the pull request's runs on that commit have finished. GitHub marks
   the pull request merged when its head reaches main.
7. **Main's checks pass.** The push to main runs `check` and publishes the documentation site.
   Wait for both.
8. **Tag.** Tag main's commit `vX.Y.Z`, matching `pyproject.toml`'s version, and push the tag.
9. **The release run passes.** The tag runs the checks again, then pushes the image, publishes
   the packages and creates the GitHub release. Wait for all of it.

## Deploying

10. **Deploy the Worker** with `make worker-deploy`, which runs `make preflight` first and
    deploys nothing if a chain isn't ready ([Deployment](../DEPLOYMENT.md)).
11. **Rehearse the verify run** on the local chain with `make anvil-verify`. It spends nothing.
12. **Verify the deployed relay** with `make sepolia-verify`. It spends Sepolia ETH and takes a
    few minutes. `make ethereum-verify` spends real money and is run only when it's needed.
13. **Check that it answers.** bruno.mailam.app's root page gives the new version and links its
    source at the tag.
