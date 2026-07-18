---
name: mod-release
description: Bump the flma mod's version, add a changelog.txt entry, verify format, and tag+push a release -- GitHub Actions (release.yml) takes it from there, re-validating, building the zip, creating the GitHub release, and publishing to the Factorio Mod Portal.
---

# flma mod release

## Purpose

Cuts a new release of the `flma` Factorio mod (`mod/`) without hand-driving
the Mod Portal's upload API or forgetting a changelog-format footgun.
Everything after the tag push is automated by
`.github/workflows/release.yml`, which re-runs the same CI gate
`.github/workflows/ci.yml` uses, builds the zip, creates the GitHub release,
and uploads to mods.factorio.com -- this skill's job stops at pushing a
correctly-prepared tag.

## When to use this skill

- The user asks to release, publish, cut a release, or bump the version of
  the flma **mod** specifically (not the Python planner side -- `planner/`
  and `src/` aren't independently versioned; only `mod/info.json` is).

## What this skill does (and doesn't do)

**Does:** bump `mod/info.json`'s version, add a `mod/changelog.txt` entry in
its exact required format, run `make quick` to catch regressions before
anything is pushed, create and push a `vX.Y.Z` git tag.

**Does NOT:** build the mod zip, create the GitHub release, or upload to the
Factorio Mod Portal -- `.github/workflows/release.yml` does all of that
automatically once the tag lands on GitHub, gated behind the same CI checks
as every other push. Don't try to replicate that pipeline manually; if it's
failing, fix the workflow or the underlying issue, not by hand-uploading.

## Steps

1. **Scope the bump.** Check what actually changed in `mod/` since the last
   tag (`git log <last-tag>..HEAD -- mod/`) to pick patch/minor/major --
   planner-only changes don't need a mod version bump at all.
2. **Bump `mod/info.json`**'s `"version"` field.
3. **Add a new entry at the top of `mod/changelog.txt`**, following the
   exact grammar documented in `mod/CLAUDE.md`'s "changelog.txt format"
   section: a 99-dash separator line, `Version: X.Y.Z`, an optional
   `Date: YYYY-MM-DD` line, a `  Changes:` category header (exactly 2-space
   indent, colon, nothing after), and `    - ` entries (exactly 4 spaces,
   dash, space) with `      ` continuation lines (exactly 6 spaces, no
   dash) for wrapped text. No tabs, no trailing whitespace anywhere.
   Verify mechanically before moving on -- Factorio's changelog parser
   silently mis-renders a malformed section rather than erroring, and CI
   will also reject this, so catch it now:
   ```bash
   python3 -c "
   lines = open('mod/changelog.txt', encoding='utf-8').read().split(chr(10))
   bad = [i+1 for i, l in enumerate(lines) if l != l.rstrip() or '\t' in l]
   print('trailing-whitespace/tab lines:', bad or 'none')
   print('bad separators:', [i+1 for i, l in enumerate(lines)
         if set(l) == {'-'} and l and l != '-'*99])
   "
   ```
4. **Run `make quick`** (lint + typecheck + tests). This is a superset of
   what CI re-checks after the tag push -- catching a failure here is far
   cheaper than after publishing is already underway.
5. **If `mod/` changed in a way `luac -p` can't validate** (new settings,
   new exported fields, anything Factorio's own engine would need to load
   to check), do a quick pass through the `factorio-dev` skill first. CI's
   Lua check is parse-only syntax validation -- it will NOT catch a
   Factorio-engine-level rejection. Real example hit during development: a
   `string-setting` with a blank default needs `allow_blank = true` or the
   mod fails to load entirely at startup, and `luac -p` says nothing about
   it.
6. **Commit** the version bump + changelog entry. Ask the user first unless
   they've already asked you to commit as part of this release (matches
   this project's normal commit conventions).
7. **Tag and push:**
   ```bash
   git tag -a vX.Y.Z -m "flma vX.Y.Z"
   git push origin vX.Y.Z
   ```
8. **Hand off.** Tell the user the tag is pushed and point them at the
   Actions run (`gh run list --workflow=release.yml` or the repo's Actions
   tab) -- GitHub Actions now owns re-running CI, building the zip,
   creating the GitHub release, and uploading to the mod portal.

## First-time setup (once, not part of a normal release)

The release workflow needs a repo secret: **`FACTORIO_API_KEY`**, generated
at https://factorio.com/profile with the **"ModPortal: Upload Mods"** scope
(not "Publish Mods" -- flma is already published; this only adds new
releases to it). Add it under the repo's Settings -> Secrets and variables
-> Actions.

Optional hardening: create a GitHub Environment named `mod-portal`
(Settings -> Environments) with yourself as a required reviewer.
`.github/workflows/release.yml` already references this environment name,
so the moment it exists with protection rules, the publish job will pause
for manual approval after CI passes but before the public, irreversible
portal upload -- no workflow changes needed to turn this on.

## Reference

- `mod/changelog.txt` grammar: `mod/CLAUDE.md`'s "changelog.txt format"
  section.
- Full release pipeline: `.github/workflows/release.yml` (invokes
  `.github/workflows/ci.yml` as a reusable gate via `workflow_call`).
- Prior release tone/format: https://github.com/jhjaggars/flma/releases, or
  `git log --oneline -- mod/changelog.txt`.
