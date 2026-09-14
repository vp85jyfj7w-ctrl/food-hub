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

Status: done, on branch foodhub/phase-2-scanning. See FOODHUB_TECHNICAL_PLAN.md section 3.2.

Additive: none (every change below extends an existing upstream file or table).

Patches to upstream files:
- service/app/models/db_models.py -- PendingItem gains a nullable retailer_id column (FK to the new foodhub_retailers table added in Phase 4), so a "Bought From" pick can ride along with a pending item from the moment it is scanned.
- service/app/database.py -- _COLUMN_ADDITIONS: pending_items gains retailer_id INTEGER (additive migration, applied automatically via ensure_schema() on next boot; no Alembic involved, matching the app's existing pattern).
- service/app/config.py -- new Settings field quick_add_mode: bool = False, added to _SAVEABLE so it persists like any other toggle.
- service/app/templating.py -- theme_context() exposes quick_add_mode to every render, the same global-flag pattern already used for printing_enabled/ai_configured, rather than threading it through one route.
- service/app/templates/add.html -- the #manage-pantry-config hidden div (the established way this page hands a couple of server-rendered values to its external, cache-busted JS) gains data-quick-add-mode alongside the existing data-printing/data-version.
- service/app/static/js/manage-pantry.js -- reads data-quick-add-mode; when on and the current mode is Inventory, the camera decode callback in startScanner() skips stopScanner() so the camera keeps rolling for the next item ("Scan Next Item"), and handleScanResult() gains an instant_added case that reports the item straight to the same on-screen recent-scans list normal scans use (rather than the pending-id-keyed trackLiveScan(), since an instant add never creates a pending row).
- service/app/routers/pending.py -- scan_barcode() gains a quick-add branch: in Inventory mode, with quick_add_mode on, a barcode that resolves via lookup_barcode() imports straight to Grocy (GrocyClient.import_item), tags the active Shopping Session's retailer if one is running (Phase 5), and returns {"status": "instant_added", ...} instead of queuing a pending row. Any lookup failure (unknown barcode, Open Food Facts unreachable, a store-assigned/random-weight code) falls straight through to the normal pending-queue path unchanged -- quick-add never causes a scan to go missing, it only ever skips a step for a barcode it already recognises. Also added the missing `import logging` / module logger this branch's warning log needed.
  Also: PendingUpdate gained retailer_id: Optional[int]; _row_dict() now includes retailer_id; commit_pending() tags the retailer against purchase history and bumps the active Shopping Session's item count on a successful commit (best-effort, wrapped in try/except so tagging can never fail a commit).
- service/app/templates/pending.html -- each pending row gained: four quick-pick best-by buttons (Today/Tomorrow/+2d/+3d, wired through the same patchItem() PATCH the date field's onchange already uses, so "the last manual edit wins" holds exactly as it does today) and a "Bought From" retailer <select> (optional, populated from GET foodhub/retailers). A new suggestRetailersFor() pass runs after every render: for a row with a barcode and no retailer yet, it asks GET foodhub/retailers/suggest and, on a hit, pre-fills and silently PATCHes the row -- a miss, a fetch error, or the user already having picked one all leave the row exactly as it was (fail-quiet by design; see Phase 9's regression check for the two paths this exercises).
- service/app/templates/setup/_pane_scanning.html + service/app/static/js/setup/panes.js + service/app/routers/setup.py -- a "Scan Next Item" toggle in the existing "Barcode scanner" panel (next to "Scan from any page"), saved through the same savePaneHardware()/SetupSaveRequest.model_dump(exclude_unset=True) path every other toggle on that panel uses. Without this the setting could only be turned on by hand-editing settings.json.

Configuration additions:
- quick_add_mode (bool, default False) -- Settings > Barcode scanner > "Scan Next Item".

Database/schema additions:
- pending_items.retailer_id (INTEGER, nullable, FK-by-convention to foodhub_retailers.id).

New API endpoints: none new (existing POST /pending/scan gains a response status; existing PATCH /pending/{id} accepts retailer_id).
New UI routes: none.

Follow-ups not yet done: none known.

## Phase 3 -- Remove / Use / Waste

Status: done, on branch foodhub/phase-3-remove-use-waste. See FOODHUB_TECHNICAL_PLAN.md section 3.3.

The Used/Wasted split, the FEFO (first-expiring-first-out) consume order, and the "Toss it" (wasted, not eaten) action all already existed upstream (services/waste.py, the /expiring/toss/{product_id} route, and Grocy's own stock-consumption ordering) and were verified working exactly as the technical plan assumed -- no changes needed there. The one real gap: the Inventory page (unlike the Expiring page) had a "Mark consumed" button but no matching "Toss" button, so an item you noticed had gone off while browsing Inventory rather than the Expiring list had no waste-tracking path short of using Expiring instead.

Additive: none.

Patches to upstream files:
- service/app/templates/inventory.html -- each row gained a "Toss (wasted, not eaten)" button beside the existing "Mark consumed" one, calling a new tossItem(productId, amount, name) that hits the same /expiring/toss/{productId} endpoint expiring.html's own tossIt() already uses. No backend change: this is purely surfacing an existing, already-correct capability on a second page.

Configuration additions: none.
Database/schema additions: none.
New API endpoints: none.
New UI routes: none.
Follow-ups not yet done: none known.

## Phase 4 -- Retailers

Status: done, on branch foodhub/phase-4-retailers. See FOODHUB_TECHNICAL_PLAN.md section 3.4.

Additive:
- service/app/services/foodhub_retailers.py -- _SEED_RETAILERS (Tesco, Sainsbury's, Aldi, Morrisons, and a handful of other common UK retailers) + seed_retailers(db) (idempotent, called once at boot); suggest_retailer(db, barcode, grocy_product_id) and record_purchase(db, retailer_id, barcode, grocy_product_id) built on real purchase history (ProductRetailer.times_seen/last_seen) rather than a fabricated barcode-prefix-to-retailer heuristic, since no reliable public GS1-based mapping like that exists; match_store_name(db, store_name) does a difflib fuzzy match (0.6 threshold; a substring match scores 0.9) so OCR noise on a receipt header ("TESCO STORES 2938", "Sainsburys Local") still resolves.
- service/app/routers/foodhub.py -- new /foodhub/ router: Retailers CRUD (GET/POST /retailers, PUT/DELETE /retailers/{id}, GET /retailers/suggest?barcode=), plus the Shopping Session and Calendar endpoints from Phases 5-6 (one router, since all of it is Food Hub's own surface area with nothing upstream to conflict with).
- service/app/templates/retailers.html -- a server-rendered CRUD page mirroring the existing templates/defaults.html exactly (table + Add/Edit Bootstrap modals), so it behaves like every other settings-style list page in the app rather than introducing a new pattern.

Patches to upstream files:
- service/app/models/db_models.py -- new Retailer (name, sort_order, active) and ProductRetailer (retailer_id, barcode, grocy_product_id, times_seen, last_seen) tables.
- service/app/main.py -- seed_retailers(db) called in lifespan() right after the existing seed_defaults(db); foodhub router included.
- service/app/navigation.py -- new "Retailers" tab (bi-shop icon), nested beside Defaults/About the same way those settings pages already sit in the nav tree.
- service/app/routers/ui.py -- GET /ui/retailers + POST create/update/delete routes, mirroring defaults_page/create_default/update_default/delete_default field-for-field (Form(...) params, ingress_redirect() on every POST).
- service/app/templates/pending.html -- the retailer-suggestion consumption side of this service: see Phase 2's entry for retailerOptions()/suggestRetailersFor(). Documented under Phase 2 since it lives entirely in that phase's file, but it is what makes "barcode suggestion" (this phase's brief) actually reach a screen.
- service/app/routers/receipt.py -- see Phase 7's entry; the other consumer of match_store_name()/record_purchase().

Configuration additions: none (Retailers/ProductRetailer rows are the persistence layer; nothing in Settings).

Database/schema additions:
- foodhub_retailers (id, name, sort_order, active) -- seeded with common UK retailers on first boot, editable/hideable from Settings > Retailers.
- foodhub_product_retailers (id, retailer_id, barcode, grocy_product_id, times_seen, last_seen) -- purchase history, one row per (retailer, barcode) pair seen.

New API endpoints:
- GET/POST /foodhub/retailers, PUT/DELETE /foodhub/retailers/{id}
- GET /foodhub/retailers/suggest?barcode=

New UI routes:
- GET /ui/retailers, POST /ui/retailers/create, POST /ui/retailers/{id}/update, POST /ui/retailers/{id}/delete

Follow-ups not yet done: none known -- "hiding a retailer keeps its history but removes it from pickers" (rather than deleting it) is documented on the Retailers page itself as the recommended alternative to Delete.

## Phase 5 -- Shopping Session

Status: done, on branch foodhub/phase-5-shopping-session. See FOODHUB_TECHNICAL_PLAN.md section 3.5.

A Shopping Session is retailer metadata layered on top of the existing "shopping" scanner mode (services/scanner_mode.py), not a fifth mode -- the four real modes (inventory/consume/shopping/audit) are unchanged.

Additive:
- service/app/services/shopping_session.py -- current(db), current_retailer_id(db), start(db, retailer_id) (raises ValueError for an unknown/inactive retailer; finishes any already-open session first, so there is never more than one active session), finish(db, session_id=None), bump_item_count(db, session_id), as_dict(session, db).
- foodhub.py additions (see Phase 4): POST /shopping-session/start, POST /shopping-session/finish, GET /shopping-session/current.

Patches to upstream files:
- service/app/models/db_models.py -- new ShoppingSession table (retailer_id, started_at, finished_at, item_count).
- service/app/routers/pending.py -- an Inventory-mode instant add (Phase 2's quick_add_mode path) and a normal commit_pending() both tag the active session's retailer onto the item and call bump_item_count(), best-effort and never able to fail the underlying scan/commit.

Configuration additions: none.

Database/schema additions:
- foodhub_shopping_sessions (id, retailer_id, started_at, finished_at, item_count).

New API endpoints:
- POST /foodhub/shopping-session/start {retailer_id}, POST /foodhub/shopping-session/finish, GET /foodhub/shopping-session/current.

New UI routes: none yet -- there is no dedicated "Shopping Session" screen; starting/finishing a session today is an API call a future Shopping-page control can wire a button to. Tracked as a Phase 9 follow-up below.

Follow-ups not yet done:
- Add a start/finish Shopping Session control to the Shopping page itself (a button plus the current retailer name/item count), rather than the session only being reachable through the API. Deferred rather than guessed at blind, the same reasoning as Phase 1's nav-bar tagline follow-up: worth doing once against the running app so the control sits naturally in the existing Shopping page layout.

## Phase 6 -- Expiry Calendar

Status: done, on branch foodhub/phase-6-calendar. See FOODHUB_TECHNICAL_PLAN.md section 3.7.

Additive:
- service/app/templates/foodhub_calendar.html -- a vanilla-JS month-grid calendar (GET foodhub/calendar per visible range) plus an iCal subscription panel (copy the feed URL, or regenerate its token). Mobile pass in Phase 9 below.
- foodhub.py additions: GET /calendar (JSON, from the live Grocy stock list), GET /calendar/expiry.ics (a hand-rolled RFC 5545 VEVENT/VCALENDAR feed -- not the `ics` PyPI package, since service/requirements.lock is hash-pinned and enforced by tests/test_requirements_lock.py, and regenerating that lock reliably was not practical here; _ics_escape/_ics_fold handle RFC 5545 escaping and line-folding), POST /calendar/token/regenerate.

Patches to upstream files:
- service/app/config.py -- new Settings field foodhub_calendar_token: str = "" (a long per-install token in the feed URL's query string, since most calendar apps cannot send cookies or custom headers -- the same pattern Nextcloud/Radicale use, and what the technical plan itself recommends).
- service/app/navigation.py -- new "Calendar" tab (bi-calendar3 icon), nested under Inventory.
- service/app/routers/ui.py -- GET /ui/foodhub/calendar.

Configuration additions:
- foodhub_calendar_token (string, generated on first "Regenerate" click, empty/unset otherwise) -- treated as a secret: the page never re-displays a token already saved, only a freshly regenerated one, and regenerating invalidates every subscription set up elsewhere (the page warns about this before confirming).

Database/schema additions: none (the feed reads live Grocy stock; nothing is cached in Food Hub's own tables).

New API endpoints:
- GET /foodhub/calendar?from=&to= (JSON), GET /foodhub/calendar/expiry.ics?token= (text/calendar), POST /foodhub/calendar/token/regenerate.

New UI routes:
- GET /ui/foodhub/calendar.

Follow-ups not yet done: none known.

## Phase 7 -- Receipt enhancements

Status: done, on branch foodhub/phase-7-receipt-retailer. See FOODHUB_TECHNICAL_PLAN.md section 3.6.

Additive: none.

Patches to upstream files:
- service/app/routers/receipt.py -- apply_receipt_prices() now looks up the receipt job's OCR'd store name (read before receipt_jobs.clear(), since clearing drops it) via foodhub_retailers.match_store_name(), and on a match calls record_purchase() for each priced line, wrapped in its own try/except + rollback so a retailer-tagging failure can never turn into a failed price application. A store name that matches nothing (an unrecognised shop, or a receipt with no legible store line at all) simply results in no retailer tag on those purchases -- prices still apply normally either way.

Configuration additions: none.
Database/schema additions: none (this phase only writes into tables Phase 4 already added).
New API endpoints: none.
New UI routes: none.
Follow-ups not yet done: none known.

## Phase 8 -- Home Assistant

Status: verified, no code changes. See FOODHUB_TECHNICAL_PLAN.md section 7.

Every Food Hub route added in Phases 2-7 (the new /foodhub/ API, /ui/retailers, /ui/foodhub/calendar) rides the same generic HA Ingress mechanism every existing route already uses: service/app/ingress.py reads X-Ingress-Path per request and exposes it as the ingress_path template global, every new page extends base.html (which sets <base href="{{ ingress_path }}/">) and uses root-relative links, and the two new POST-redirect routes in routers/ui.py call the existing ingress_redirect() helper exactly like defaults_page's routes do. None of this is a route allowlist or a per-page opt-in, so nothing needed registering for Ingress specifically. The one deliberate exception is the calendar's .ics feed (foodhub_calendar.html's FOODHUB_ORIGIN construction, and the feed URL a phone/desktop calendar app subscribes to): that URL is fetched directly by an external calendar client, never through the HA Ingress proxy, which is exactly why it carries its own long-lived token instead of relying on Ingress or a session cookie for auth. No other Home Assistant integration (ha_events.py, gadgets_ha.py, entity discovery) references anything Food Hub added, so there was nothing else to check.

## Phase 9 -- Polish

Status: done, on branch foodhub/phase-9-polish.

- Mobile pass: templates/foodhub_calendar.html's 7-column month grid gets a max-width:480px media query (smaller min-height, padding, and font-size per cell) so it fits a phone width without the page scrolling horizontally; every other new/changed page in Phases 2-7 (retailers.html, pending.html's new controls, inventory.html's new button) already used the same flex-wrap/table-responsive/btn-group-sm patterns the rest of the app relies on for narrow screens, so no changes were needed there.
- Empty/error states: the calendar's fetch failure path (calAlert) and the retailer-suggestion fetch failure/no-match path (pending.html's suggestRetailersFor -- a miss returns {"suggestion": null} and a network error is caught) were both exercised directly against the running app (a barcode with no purchase history, and a barcode with prior history at a known retailer) rather than only reasoned about; both leave the page in a normal, unbroken state.
- Regression coverage: ran the app's existing test suite (pytest) after every change in Phases 2-7 -- tests/test_navigation.py needed one update (the new Calendar tab's exact nested-children list), tests/test_smoke_routes.py, test_settings_save_safety.py, test_rendered_js_syntax.py-equivalent inline/external JS parses (verified directly with `node --check` against both the raw external files and every changed page's actual rendered output, since the checked-in test module itself depends on an outbound AI call this sandbox cannot make), and all of Phases 2-7's own new endpoints were exercised end-to-end via FastAPI's TestClient (retailer CRUD, shopping session start/finish, calendar JSON + .ics feed with a good and a bad token, quick-add's success path with a mocked lookup and its fallback path with lookup failing, and the default-off regression case) with no unexplained failures.
- Backup/restore rehearsal: not run in this fork -- the technical plan's rehearsal step needs an actual install with real Grocy data and is explicitly a "do this against the live NAS deployment" step, not something to fake against an empty local clone. Tracked as a deployment-time follow-up, not a code gap.
- NTS dashboard reminder: once Food Hub is actually deployed on the UGREEN NAS, remember to add it to the household's Port Register / Server Services dashboard (per Will's own standing note on tracking self-hosted services), alongside Grocy. Not a code change; noted here so it isn't forgotten at deploy time.

Follow-ups not yet done (carried forward from Phases 2-9, collected here for one place to check before/after deploying):
- Phase 1: set nav_hidden (or seed settings.json) to hide tt_timers, tt_thermo, tt_both, weather, camera; the code default already does this for a *fresh* install (see Phase 1's entry and config.py's nav_hidden default), so this line only matters for an existing install upgrading into Food Hub.
- Phase 1: decide whether APP_TAGLINE should render in the navbar brand, once the layout can be checked visually.
- Phase 5: add a start/finish Shopping Session control to the Shopping page UI (today it is API-only).
- Deploy time: run the backup/restore rehearsal against the real NAS install; add Food Hub to the Port Register / Server Services dashboard.

## Merging upstream Pantry Raider updates

1. Back up service/data/ and grocy/config/ before touching anything.
2. git fetch upstream, then git merge upstream/main (or rebase the current foodhub/phase-N branch on it), one Food Hub phase branch at a time.
3. Re-check the patched files listed above first, in particular: config.py, templating.py, about.html (Phase 1); models/db_models.py, database.py, main.py, navigation.py, routers/pending.py, routers/receipt.py, routers/ui.py, routers/setup.py, static/js/setup/panes.js, static/js/manage-pantry.js, templates/add.html, templates/pending.html, templates/inventory.html, templates/setup/_pane_scanning.html (Phases 2-7) -- these are the files most likely to conflict.
4. Re-run the acceptance test in FOODHUB_TECHNICAL_PLAN.md section 44 before calling the merge done.
5. Record the new upstream commit/tag at the top of this file.

