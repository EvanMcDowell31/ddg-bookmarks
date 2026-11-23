# ddg-bookmarks

Read, add and remove bookmarks in **DuckDuckGo for Mac** from Claude Code while the browser runs, with a backup before every change.

It's a Claude Code plugin and an Irie plugin, both around one small Python script that you can also run by hand. Site: https://evanmcdowell31.github.io/ddg-bookmarks/

> An independent project, not made or endorsed by DuckDuckGo. "DuckDuckGo" is used only to say which browser it works with.

## What you get

| | What it does |
|---|---|
| `/ddg-bookmarks:add <links> [into <folder>]` | Bookmarks links in one step. It checks each one loads, skips any already bookmarked, and files the rest into the folder you name. |
| `/ddg-bookmarks:bookmark-projects` | A workflow. Agents find your GitHub repositories pushed in the last year and the sites they publish, and, if you ask, the web apps running on your Mac. Then one checks each link and merges the new ones into a folder (default `Projects`). It leaves out forks, archived repositories, your profile repository and dotfiles, and only adds: it never removes, renames or moves anything. |
| `ddg-bookmarks` | The command both use. `tree`, `find`, `add`, `remove` and `where`, on the PATH while the plugin is enabled. |
| The `bookmarks` skill | How the store works, so Claude can do anything else you ask of your bookmarks. |

## Install

In Claude Code:

```
/plugin marketplace add EvanMcDowell31/ddg-bookmarks
/plugin install ddg-bookmarks@ddg-bookmarks
```

Or from a shell: `claude plugin marketplace add EvanMcDowell31/ddg-bookmarks`, then `claude plugin install ddg-bookmarks@ddg-bookmarks`.

It needs macOS, DuckDuckGo for Mac (the direct download or the Mac App Store build), and Python 3, which macOS's command line tools include. On macOS 14 and later, the first read asks you to let your terminal access data from other apps. A terminal with Full Disk Access isn't asked.

**In [Irie](https://github.com/irie-people/irie):** clone this repository, copy [`irie/ddg-bookmarks.toml`](irie/ddg-bookmarks.toml) to `~/.config/irie/plugins/`, set its `path` to your clone's `skills/bookmarks`, and turn the plugin on in Settings › Plugins. Sessions then get `/irie:ddg-bookmarks`, and `:bookmark https://example.com into Reading` in a task bar starts one.

## Examples

```
/ddg-bookmarks:add https://example.com/docs into Work > Docs
/ddg-bookmarks:bookmark-projects
/ddg-bookmarks:bookmark-projects into Code, since 2025-06-01, local apps too, and propose without adding
```

The command by hand:

```
ddg-bookmarks tree                                   # the bookmarks bar
ddg-bookmarks tree "Work" --json > work.json         # a folder, as a spec
ddg-bookmarks find example.com                       # what's bookmarked with that in it
ddg-bookmarks add --title "Docs" --url https://example.com/docs --into "Work" --merge
ddg-bookmarks add work.json --into "Archive" --merge --dry-run
ddg-bookmarks remove "Work > Old link"
```

Outside Claude Code, run the script itself: `python3 -I skills/bookmarks/scripts/ddg_bookmarks.py tree`.

A **spec** is a JSON list: `{"title": "T", "url": "U"}` for a bookmark, and `{"title": "T", "children": [...]}` for a folder. A **path** is titles from the bar down, joined by ` > `. `--merge` makes `add` safe to repeat: a folder that's already there takes only the children it lacks, and a bookmark already there, by title or by link, is skipped.

## When changes show

DuckDuckGo reads its bookmarks again when you next bookmark, edit or move something in it, or when it next starts. Until then, new bookmarks are saved but not yet shown. Nothing here restarts the browser.

## How it stays safe

- **A backup first.** Every write first saves a one-file copy beside the store, `Bookmarks.sqlite.bak-<UTC time>`. A refused command, a `--dry-run` and an add with nothing to add write nothing and leave no backup.
- **Written as the app writes.** DuckDuckGo keeps bookmarks in a Core Data SQLite store and reads it afresh each time. New rows get what the app's own `BookmarkEntity` gives them: an upper-case UUID, `Z_OPT` 1 and a modified time. The folder that changes gets its `Z_OPT` bumped, and Core Data's `Z_MAX` moves on. A removal marks rows for deletion, as the app's own removal does, and the app clears them itself.
- **All or nothing.** Each change is one `BEGIN IMMEDIATE` transaction that ends with an integrity check.
- **Strict about what it opens.** It opens only an existing store, checks it's DuckDuckGo's (its tables, WAL mode), and refuses everything else. It never creates a database where a path was mistyped, and an empty `--db` or `DDG_BOOKMARKS_DB` is refused rather than read as "use the browser's own".
- **Only web links.** `javascript:`, `data:` and other links that run code are refused.

To undo an add, `remove` what it added. Don't copy a backup over the store while DuckDuckGo runs.

## Limits

- macOS only, DuckDuckGo only.
- With DuckDuckGo Sync on, adds and removals are written as the app writes its own, but that's untested. The script prints a note when the store shows Sync has run.
- A future DuckDuckGo could change its store. The script refuses a store whose tables it doesn't know rather than guess.

## Development

```
python3 -m unittest discover -s tests     # 47 tests, against stores built with DuckDuckGo's schema
claude plugin validate .
```

[PROVENANCE.md](PROVENANCE.md) lists every source this was built from. Licensed under [Apache-2.0](LICENSE).
