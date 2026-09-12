# Food Hub — Changes from Upstream

Running log of how Food Hub diverges from upstream Pantry Raider. Update this file in the same commit as any change it describes, not after the fact.

Upstream: https://github.com/Syracuse3DPrintingOrg/PantryRaider (forked at commit 6ec7306, tag 0.19.2, 2026-09-12)

## How to read this file

Each change lists: what it is, which files it touches, and whether it is additive (new file/route, safe on any upstream merge) or a patch to an existing upstream file (worth re-checking after every upstream pull). Every entry also gets a matching commit on the foodhub/phase-N-* branch named in Implementation phases below.

## Phase 1 -- Branding

Status: done, on branch foodhub/phase-1-branding.

Additive:
- .env.example -- new Branding section documenting APP_NAME and APP_TAGLINE.

Patches to upstream files:
- service/app/config.py -- APP_NAME and the new APP_TAGLINE now read from environment variables (APP_NAME, APP_TAGLINE), defaulting to upstream's own "Pantry Raider" / empty string when unset. Everywhere else in the app that already used config.APP_NAME is unaffected.
- service/app/templating.py -- APP_TAGLINE is imported and added to the Jinja context as app_tagline, alongside the existing app_name.
- service/app/templates/about.html -- page title now interpolates {{ app_name }} instead of a hardcoded "Pantry Raider"; a credit note was added above the existing open-source credits, stating Food Hub is a personal fork of Pantry Raider and that all credit for the underlying application belongs to the Pantry Raider project. No existing credit or licensing text was removed.

Configuration additions:
- APP_NAME (env var) -- display name shown in the nav brand, page titles, and the About page. Default: Pantry Raider.
- APP_TAGLINE (env var) -- short tagline, currently surfaced on the About page context only. Default: empty (not shown).

Database/schema additions: none.
New API endpoints: none.
New UI routes: none.

Features hidden from navigation: none yet -- hiding Timers, Thermometers, Camera and Weather (brief section 7/31) is a Settings > Personalization > Appearance change (nav_hidden), not a code change, and is best done once against a live install rather than blind in this fork. Tracked as a Phase 1 follow-up below.

Follow-ups not yet done:
- Set nav_hidden (or seed settings.json) to hide tt_timers, tt_thermo, tt_both, weather, camera on first deploy.
- Decide whether APP_TAGLINE should also render in the navbar brand (base.html) once the app is running and the layout can be checked visually; deferred in Phase 1 to avoid a blind edit to the kiosk nav bar.

## Phase 2 -- Streamlined scanning

Status: not started. See FOODHUB_TECHNICAL_PLAN.md section 3.2.

## Phase 3 -- Remove / Use / Waste

Status: not started. See FOODHUB_TECHNICAL_PLAN.md section 3.3.

## Phase 4 -- Retailers

Status: not started. See FOODHUB_TECHNICAL_PLAN.md section 3.4.

## Phase 5 -- Shopping Session

Status: not started. See FOODHUB_TECHNICAL_PLAN.md section 3.5.

## Phase 6 -- Expiry Calendar

Status: not started. See FOODHUB_TECHNICAL_PLAN.md section 3.7.

## Phase 7 -- Receipt enhancements

Status: not started. See FOODHUB_TECHNICAL_PLAN.md section 3.6.

## Phase 8 -- Home Assistant

Status: not started. See FOODHUB_TECHNICAL_PLAN.md section 7.

## Phase 9 -- Polish

Status: not started.

## Merging upstream Pantry Raider updates

1. Back up service/data/ and grocy/config/ before touching anything.
2. git fetch upstream, then git merge upstream/main (or rebase the current foodhub/phase-N branch on it), one Food Hub phase branch at a time.
3. Re-check the patched files listed above first -- config.py, templating.py, about.html -- since they are the files most likely to conflict.
4. Re-run the acceptance test in FOODHUB_TECHNICAL_PLAN.md section 44 before calling the merge done.
5. Record the new upstream commit/tag at the top of this file.

