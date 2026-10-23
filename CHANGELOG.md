# Changelog

## 1.2.0 — the sayngwyn look

- The window opens in the sayngwyn colours (black and deep red, from the author's character sheet, matching their other
  mods). Red marks only what's selected and the Download button; everyday buttons are silver outlines.
- While nothing is selected, the details pane shows the sayngwyn mark, faded, above the hint, set in the Sayngwyn Serif
  font (included, under the SIL Open Font License). Lists, filters and buttons stay plain.
- **Theme** picker in the window, the same four as the author's other plugins: **sayngwyn** (the default), **Light**,
  **Dark** (both plain, with a neutral blue accent), and **Match MO2** (the window looks exactly as your MO2 theme draws it,
  as before). The window's own themes look the same whatever theme MO2 runs. The choice is remembered in MO2's plugin
  settings. In the window's own themes the right-click menu's icons take the theme's text colour, so they read in each.
- Ticked boxes are silver with a bone tick; menus, drop-down lists and tooltips sit on a raised dark colour with a border;
  scrollbars are slim and silver.
- Problems on the status line now show a ⚠ sign and a distinct pink-red colour, so they never read as ordinary updates.
- Every text colour reads at 4.5:1 or better on every background it can sit on (checked by the tests).

## 1.1.0 — search, sort and filters

- Filters above the list, each re-running the current search: **PC versions only** (on by default; console-only Creations are
  left out), **Free only** (on by default; store items are left out) and a **Category** picker (loaded from Bethesda once per
  game when you first open it or first search). A Creation still counts as free only by this plugin's own check.
- An empty search now browses the whole catalogue for the game, 50 at a time, with a **Show more** button.
- **The window opens on a list: Most downloaded.** The first time it is shown it browses by itself. Bethesda has no sort for
  downloads or likes, so **Most downloaded** and **Most liked** read Bethesda's 1,000 most popular Creations (at most ten
  requests, one after the other, with progress in the status line), sort them here and show 50 at a time; **Show more** reveals
  more from what was read. What was read is kept while the window lives (per game, PC and Free filters, category and author),
  so reopening it, changing between Most downloaded and Most liked, or returning to a list you have seen makes no request.
- **What's hot:** the "Popular in" picker is gone. **Trending this week**, **Popular this month** and **Most popular (all time)**
  are Bethesda's popularity sorts (a page at a time); they are greyed out with search words, which Bethesda ignores sorting for.
  The sort list is now Best match (words only), Most downloaded, Trending this week, Popular this month, Most popular (all time),
  Most liked, Best rated, Newest, Recently updated, Title A-Z.
- **Verified creators only** and **Official creations only** (both off by default): only Creations by Bethesda's Verified Creators,
  or published by Bethesda. Bethesda has no such filters, so they are applied here: with either on a browse reads up to 1,000
  Creations first (still ten requests at most), and a search with words filters its 500 matches. Turning them on or off with
  the Creations already read re-filters at once, without a request. Rows show "✓ Verified" / "✓ Official" after the author.
- A search with words reads up to the 500 best matches (at most five requests, one after the other) and sorts them here, so
  changing the sort is instant. When there are more than 500 matches the status line says so.
- Each row shows total downloads (all platforms), likes and the last update, and **Most downloaded** sorts by that same total;
  the details panel shows PC and all-platform downloads, likes, a rating word, the published and updated dates and the categories.
- Creations in Bethesda's in-game store show their **price in Creation Credits** (PC store, with sales), or "Claim in game (free)"
  for store items claimed at no cost, in the list and the details panel. They are still never downloaded here.
- The free / price marker leads each row, in bold. The Search button reads **Browse** while the box is empty. Status counts
  use thousands separators, and the details show one download figure when PC and all-platform counts are the same.
- Clearer messages: no internet ("Couldn't reach Bethesda's servers"), Bethesda asking to slow down, links in plain words.
  A creations.bethesda.net link for the other game switches the Game picker to it.
- A refused download names the file that isn't game data. A few more inert game-data types are accepted (.hkx, .tri, .egm,
  .egt, .bik, .seq, .lod, .btr, .bto, .btt, .ssf, .cdf, .gid).
- The chosen sort is kept when you switch between browsing and searching, when that sort is offered in both.
- The window fits the screen on scaled laptop displays, the selected row stays readable when the window isn't focused, and
  the progress bar and Cancel appear only while downloading.
- **More by this author** in the details panel lists that author's Creations, including the one you are looking at (the filters
  still apply).
- Changing a filter or the sort searches after a short pause (about 0.3 s) that each further change restarts, so stepping
  through a list asks Bethesda once.
- **In your game.** Each game keeps a list of the Creations it downloaded through its in-game Creations menu
  (`ContentCatalog.txt` in your local app data folder; for Skyrim also the GOG and Epic folders). Creations Browser reads it
  (read only; at most 5 MB and 2,000 entries, and a broken file is tolerated) and marks those Creations **In your game** in
  the list and the details, in place of their price, "Get it in game" or "Free". A free one can still be downloaded for MO2.
  The list is read once per window session and again when you reopen the window or change the game.
- **In my game only**, a new checkbox (off by default): shows exactly the Creations the game's list names, free or paid, looked
  up by id (at most 300, in at most three requests of 100; more than 300 and it says so). PC, Free, category and the creator
  filters are greyed out while it is on; search words filter by title and every sort works on your PC. It is cached like the
  other lists, so turning it off and on again asks nothing new. With nothing in the game's list, or no list found, it says so.
  A Creation you own but never downloaded in game can't be seen without logging in, which this plugin never does.
- **Changing the Game picker now opens that game's list by itself** (its default browse, or its In my game view when that is
  on), after the usual short pause and from the cache when you have seen it before, instead of waiting for you to search again.
- README: "What it sends" now covers the In my game lookup, and a new "What it reads on your PC" section lists what is read.

## 1.0.0 — first build (not published)

- Mod Organizer 2 tool plugin (Tools > Creations Browser) for Fallout 4 and Skyrim Special Edition.
- Search Bethesda's public Creations catalogue, or paste a creations.bethesda.net link or Creation id.
- Clear Free / Get it in game markers; paid, store, withdrawn and not-clearly-free Creations are never downloaded.
- Details: author, version, size, description, release notes for the PC version, required DLC, required Creations
  (with "Download these too" for the free ones), preview picture (fetched only for the Creation you select) and a link to
  the Creation's page.
- Downloads the PC file of a free Creation, verifies its size and checksum, accepts only game data under `Data`, and
  saves it as an ordinary zip with a `.meta` file in MO2's Downloads folder. Existing downloads are reused.
- Shows Downloaded, Installed and Update available next to each Creation.
- One download at a time, with progress and Cancel; closing the window cancels cleanly.
- Runs nothing in the background; MO2's own enabled switch (Settings > Plugins) turns it off.
