# kindleone — ePub on a jailbroken Kindle

Read Z-Library ePubs directly on a Kindle via KOReader, with an MCP server that
sends books over USB. **No format conversion anywhere** — KOReader reads ePub natively,
so Amazon's MOBI/AZW round-tripping is out of the picture entirely.

## Your device

| | |
|---|---|
| Model | **Kindle Paperwhite 3** (7th gen, 2015) — per KOReader's own detection, `KindlePaperWhite3` |
| Mount point | `/Volumes/Kindle` |
| Firmware | `5.16.2.1.1 (409747 002)` |
| Storage | 4 GB (3113 MB volume), USB Mass Storage |
| Jailbreak | **WinterBreak v2.1.0** (works; patched only at firmware ≥ 5.18.1) |
| KOReader build | **`kindlepw2`** — `kindlehf` requires firmware ≥ 5.16.3, yours is 5.16.2.1.1 |

Because the Kindle mounts as a normal USB disk rather than MTP, it is 10th-gen or older.

> **Do not let this Kindle take a firmware update.** 5.18.1 and later have no public
> jailbreak. Before the jailbreak, storage was filled so an OTA update could not download;
> since then `jb.sh` blocks updates (the device has been online repeatedly and stayed on
> 5.16.2.1.1).

## Two corrections to the original plan

- **KyBook 3 cannot be installed.** It is an iOS-only app; there is no Kindle build.
  KOReader is the ePub reader for a jailbroken Kindle, and it is what the
  `qiaomu-simpleui` plugin extends.
- **`joeseesun/qiaomu-simpleui` is not a jailbreak tool.** It is a KOReader plugin that
  replaces the home screen and adds an RSS reader. Its own README says "no jailbreaking."
  It installs *last*, after KOReader.

## Install order

### 1. Block firmware updates before jailbreaking

Fill storage with filler files leaving ~80 MB free, per the
[KindleModding guide](https://kindlemodding.org/jailbreaking/prevent-auto-update/).
An OTA update needs more free space than that, so it cannot land. Remove the filler
once jailbroken.

### 2. Jailbreak with WinterBreak

On the Kindle: **Airplane mode ON**, then reboot, then reconnect USB.
Copy the payload (see `scripts/` or re-run the copy step), eject, then on the Kindle:

1. Tap the **cart / Kindle Store** icon on the home screen.
2. Tap **Yes** when asked to turn off Airplane mode.
3. When Mesquito loads, tap the **WinterBreak** icon.
4. Wait ~30 s — text appears and the UI restarts. If no text appears, repeat.

Then reboot and delete `fill_disk/` plus any `update*.bin` at the root.
WinterBreak blocks updates itself from this point on.

### 3. Install KOReader (via KPM, pre-installed by WinterBreak)

KUAL is **obsolete** and no longer works. Use the search bar on the home screen:

```
;kpm update
;kpm install koreader
```

A new "book" icon appears — tap it to launch, or run `;kpm launch koreader`.
KPM picks the correct build for your firmware automatically.

### 4. Install the SimpleUI plugin

Copy `simpleui.koplugin/` into `/koreader/plugins/` over USB, then restart KOReader.
Requires KOReader ≥ v2026.03 with CoverBrowser enabled.

**Use upstream, not the fork.** `joeseesun/qiaomu-simpleui` has **zero stable releases** —
all five of its releases are flagged `prerelease`, and its README states it was tested only
on a Paperwhite 4. This device is a **Paperwhite 3**, a generation older.

Upstream [`doctorhetfield-cmd/simpleui.koplugin`](https://github.com/doctorhetfield-cmd/simpleui.koplugin)
has 23 stable releases (latest **3.0.0**, 2026-10-24) and a far larger user base, so that is
what is installed here. The fork's extras — 乔木 RSS feed, AI translate/Q&A, WeRead entry —
are the only thing given up.

Switching away from the fork means clearing `koreader/settings/simpleui/`: the fork runs
settings migrations up to v13 that rename keys upstream 3.0 does not recognise.

## The MCP server

`kindle_mcp/server.py` — zero third-party dependencies, stdio JSON-RPC. Registered as:

```
claude mcp add kindle --scope user -- /usr/bin/python3 \
  /path/to/kindleone/kindle_mcp/server.py
```

### Tools

| Tool | Purpose |
|---|---|
| `kindle_status` | Mount point, firmware, free space, whether KOReader/plugins are installed |
| `find_ebooks` | Find recent ebooks in the Apple Books iCloud library, `~/Downloads`, `~/Desktop`, `~/Documents` |
| `send_to_kindle` | Copy ebooks to the Kindle, cleaning up filenames |
| `list_kindle_books` | List what is already on the device |
| `send_to_inbox` | **Wireless**: upload books to the cloud inbox (`/Kindle`) that KOReader syncs from |
| `list_inbox` | List what is waiting in the cloud inbox |

Just ask in plain language: *"send the book I just downloaded to my Kindle."*

### Books go in `books/`, not `documents/`

Kindle's native firmware cannot open USB-copied ePubs, so anything in `documents/`
clutters the native home screen as unreadable items — which is exactly the problem
you started with. Books land in `/mnt/us/books`, which KOReader browses directly
and the native library ignores.

### Filename cleanup

Z-Library filenames are rewritten from the ePub's own OPF metadata, with
provenance junk (`(Z-Library)`, `_z_lib_org`, `-cdeKey_…`, `_B0…`) stripped:

```
专业投机原理_珍藏版_华章经典金融投资系列_etc_z_lib_org.epub
  -> 专业投机原理（珍藏版） (华章经典·金融投资系列) - （美）维克·托斯波朗迪（Victor Sperandeo）.epub

The Psychology of Money Timeless Less... (z-lib.org).epub
  -> The Psychology of Money- Timeless Lessons on Wealth, Greed, and Happiness - Morgan Housel.epub
```

Placeholder metadata (`[No data]`, `Administrator`, `未知作者`, `Unknown`) is rejected in
favour of the filename, and the filename is preferred outright when it is strictly more
informative than the metadata title. Names are made FAT32-safe while preserving CJK.

### Configuration

| Env var | Default |
|---|---|
| `KINDLE_MOUNT` | auto-detected via `system/version.txt` under `/Volumes` |
| `KINDLE_BOOKS_SUBDIR` | `books` |
| `KINDLE_SOURCE_DIRS` | `~/Downloads:~/Desktop:~/Documents` |
| `KINDLE_SCAN_DEPTH` | `3` |

## Wireless delivery via a cloud inbox

```
iPhone: Telegram bot -> Share -> cloud app, folder /Kindle -------+
Mac:    "send the LLM books to my Kindle" -> MCP send_to_inbox ---+--> inbox /Kindle
                                                                        |
             Kindle on Wi-Fi: KOReader -> Tools -> Cloud storage+ -> long-press -> Sync now
```

The Kindle can be asleep when a book is sent; it waits in the inbox until KOReader syncs.
The backend is `KINDLE_INBOX` in `.env`: `webdav` (default) or `dropbox`.

### Why not Dropbox on the Kindle

Dropbox was set up first and works from the Mac, but the Kindle's network is behind the GFW.
The router's DNS returns poisoned answers for `api.dropboxapi.com` (an unrelated IPv4 and a
Facebook `face:b00c` IPv6), so KOReader logs
`DropBoxApi: cannot get access token: Address family not supported by protocol`: the bogus
IPv4 fails, then the bogus IPv6, which the PW3 kernel cannot use. The phone and Mac reach
Dropbox only through a proxy (Shadowrocket); KOReader on the Kindle cannot use one.

The Dropbox code stays in the MCP (8 MB chunked upload sessions with per-chunk retry) for
networks where it is reachable. Its credentials were removed from the Kindle.

### WebDAV via Jianguoyun (坚果云)

`dav.jianguoyun.com` resolves cleanly through the router and answers directly, without a
proxy, in under 0.2 s. KOReader's **Cloud storage+** plugin supports WebDAV *with* folder
sync (the older built-in Cloud storage only syncs Dropbox). Both share
`koreader/settings/cloudstorage.lua`; a WebDAV entry there holds `address`
(`https://dav.jianguoyun.com/dav`), `username`, `password` (a Jianguoyun third-party *app*
password, not the login password), `url`, and `sync_source_folder` /
`sync_dest_folder` (`/Kindle` -> `/mnt/us/books`). It is written over USB from `.env`
rather than typed on the Kindle keyboard, and sits in plain text on the Kindle's
USB-visible storage. The free Jianguoyun plan has monthly upload/download quotas.

Sync downloads only files missing locally or with a different size.

### Sources

- **Apple Books iCloud library** (`~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents`)
  is the first source directory. Anything opened in Books on the iPhone/iPad syncs here.
  Books stores ePubs as unzipped *folders*; the MCP repacks them into valid `.epub` files
  (`mimetype` first and stored, Apple's `iTunesMetadata.plist` dropped).
- Telegram on the Mac keeps no ebooks in its cache: books from the Telegram bot reach the
  Mac only by way of Apple Books.

### Credentials

Every secret lives in `.env`, which is gitignored; `.env.example` shows the shape.

### Kindle-side KOReader settings (configured over USB)

`settings.reader.lua` changes: `home_dir` = `/mnt/us/books`, `lock_home_folder` = false (it
was locking KOReader inside Books), `start_with` = `last` (reopen the last book), folder
shortcuts for `books/` and `documents/`, and SimpleUI disabled via `plugins_disabled`
(re-enable under Tools -> Plugin management).

The native-library launcher is `documents/KOReader.sh`, a KPM scriptlet that runs
`kpm launch koreader`.

### Reading lists

`lists/*.txt` hold Apple Books entry names, one per line, for batch sends. They are
gitignored: they describe a personal library, and some entry names carry the original
buyer's name stamped into early-release ebooks.
