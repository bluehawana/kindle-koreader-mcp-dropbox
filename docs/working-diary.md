# Working Diary — kindleone

A running record of how ePub reading on a jailbroken Kindle Paperwhite 3 was put together:
what was done, why, what hurt, and what is still unverified. `README.md` is the reference
manual; this file is the history.

## 2026-10-27 — from a locked Kindle to an ePub reader with its own MCP server

> **TL;DR:** Jailbroke a Paperwhite 3 (WinterBreak 2.1.0), installed KOReader via KPM, and built a
> zero-dependency MCP server that sends ePubs to the device — over USB, and over the air through a
> cloud inbox. No format conversion anywhere: KOReader reads ePub natively, so Amazon's
> MOBI/AZW round-trip is out of the picture. Two of the original plan's assumptions were wrong and
> got corrected (KyBook 3 has no Kindle build; qiaomu-simpleui is a KOReader plugin, not a jailbreak
> tool). The wireless path is **not finished**: Dropbox works from the Mac but the Kindle's network
> DNS-poisons it, so the backend was switched to Jianguoyun WebDAV — and that account does not exist
> yet, so the 25-book shelf currently sits in a Dropbox folder the Kindle cannot reach.

### Session 1 (09:58–13:00) — jailbreak, and the first MCP server

**What:** Identified the device from its own files: Paperwhite 3 (7th gen, 2015), firmware
`5.16.2.1.1 (409747 002)`, 4 GB, mounted as plain USB mass storage at `/Volumes/Kindle` (which is
itself the proof it is 10th-gen or older — newer Kindles use MTP). Blocked OTA updates first by
filling storage to ~80 MB free per the KindleModding guide, then jailbroke with **WinterBreak
v2.1.0**: airplane mode on, reboot, copy the payload, eject, Kindle Store icon → Mesquito →
WinterBreak. Afterwards deleted the filler and `update*.bin`; the jailbreak's own `jb.sh` blocks
updates from then on. KUAL is obsolete, so KOReader went in through **KPM** from the home-screen
search bar (`;kpm update`, `;kpm install koreader`) → KOReader 2026.7.2, kpm 0.2.4. Then wrote the
first version of `kindle_mcp/server.py`: stdio JSON-RPC, no third-party dependencies, four tools
(`kindle_status`, `find_ebooks`, `send_to_kindle`, `list_kindle_books`).

**Why:** The native firmware cannot open a USB-copied ePub, which is the whole problem the user
started with. KOReader can, so the job is only to get books into a folder KOReader browses.

**Decisions:**
- **Books land in `/mnt/us/books`, not `documents/`.** Anything in `documents/` shows up on the
  native home screen as an unreadable item — exactly the clutter to avoid. KOReader browses `books/`
  directly and the native library ignores it.
- **Filenames are rebuilt from the ePub's own OPF metadata**, with provenance junk stripped
  (`(Z-Library)`, `_z_lib_org`, `-cdeKey_…`, `_B0…`, O'Reilly's `(for <buyer>)` stamp). Placeholder
  metadata (`[No data]`, `Administrator`, `未知作者`, `Unknown`) is rejected in favour of the
  filename, and the filename wins outright when it is strictly more informative. Names are made
  FAT32-safe while preserving CJK.
- `kindlepw2` is the right KOReader build — `kindlehf` needs firmware ≥ 5.16.3, this device is
  5.16.2.1.1. KPM picks it automatically.

**Hard parts:** The device spent most of this session looking broken. It kept falling back to
"USB drive mode" and then to the "Welcome to Kindle — register your Kindle" screen, which looked
like the jailbreak had failed; a 37 MB `zh-Hans.font` install reported itself as "updating fonts,
this may take a few minutes" and looked like a firmware flash. Both were benign. KOReader only
appeared as a home-screen icon after the install hooks finished and the UI restarted.

**Status:** Verified by copying one real book (`纳瓦尔宝典…`, 651,445 bytes, zip integrity OK) and
checking it on the device; the macOS `._` sidecar files that the first copy left behind are now
cleaned up by the server.

### Session 2 (16:48–17:45) — making it launchable, and finding the real book source

**What:** Made KOReader reachable without a ritual: `documents/KOReader.sh`, a KPM scriptlet that
runs `kpm launch koreader`. Edited `settings.reader.lua` over USB — `home_dir` = `/mnt/us/books`,
`lock_home_folder` = false (it had been locking KOReader inside Books), `start_with` = `last`,
folder shortcuts for `books/` and `documents/`. Backups of every file touched went to `backup/`.
Then traced where the books actually are: the **Apple Books iCloud library**
(`~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents`), which is now the first source
directory. Wrote `lists/llm.txt`, the 25-book LLM shelf (~297 MB), as a batch-send list.

**Why:** The user downloads from a Z-Library Telegram bot on the phone and opens the file in Books;
that is what syncs to the Mac. Telegram on the Mac keeps no ebooks in its cache at all, so Books is
the only path.

**Decisions:**
- **SimpleUI: use upstream, not the fork.** `joeseesun/qiaomu-simpleui` has zero stable releases
  (all five are prereleases) and its README says it was tested on a Paperwhite 4 — a generation
  newer than this device. `doctorhetfield-cmd/simpleui.koplugin` has 23 stable releases (3.0.0,
  2026-10-24). The fork's extras (RSS feed, AI translate/Q&A, WeRead) are what is given up.
  Switching away from the fork also means clearing `koreader/settings/simpleui/`: the fork runs
  settings migrations up to v13 that rename keys upstream 3.0 does not know.
- SimpleUI is currently **disabled** via `plugins_disabled` — one variable at a time while the
  reading path was being proven.

**Hard parts:** Apple Books does not store ePubs as files. It unzips each one into a folder, so a
naive copy produces something KOReader cannot open. The server now repacks those folders into valid
`.epub` archives (`mimetype` first and stored uncompressed, Apple's `iTunesMetadata.plist` dropped).
Tested on two books; both pass format validation.

### Session 3 (17:32–18:50) — the wireless inbox, and the firewall

**What:** Put every credential in a gitignored `.env` with a `.env.example` shape (the user's
standing rule). Registered a Dropbox app, exchanged the refresh token, taught the server to upload
in 8 MB chunked sessions, and **uploaded all 25 books to Dropbox `/Kindle`** — 25/25 OK. Wrote the
app key, secret and refresh token into the Kindle's `koreader/settings/cloudstorage.lua` so KOReader
could pull them down itself.

**Why:** The point of the whole exercise is "download on the phone, the Kindle fetches it itself" —
no cable, and the Kindle may be asleep when a book is sent.

**Hard parts — this is where the day turned:** On the Kindle, Cloud storage → Dropbox → *Sync now*
did nothing at all: no progress, no error, an empty folder. The KOReader log gave it away —
`DropBoxApi: cannot get access token: Address family not supported by protocol`, a bogus address
from a poisoned DNS answer. The Mac and the phone reach Dropbox only because they run Shadowrocket;
the Kindle has no proxy and goes straight out over the local Wi-Fi. Routing the Kindle through the
Mac as a gateway was considered and **rejected**: it would mean the Mac must always be awake with
the proxy up, which destroys the "phone downloads, Kindle fetches" property that justified the
design.

**Decisions:**
- **Switch the inbox backend to Jianguoyun WebDAV.** `dav.jianguoyun.com` resolves to its real
  address (`103.52.173.35`) through this router and answers directly in 0.18 s, no proxy.
- Use KOReader's **Cloud storage+** plugin, which supports WebDAV *with* folder sync; the older
  built-in Cloud storage only syncs Dropbox. Both share `settings/cloudstorage.lua`, and the WebDAV
  entry's fields were confirmed from the plugin source: `address`, `username`, `password` (a
  Jianguoyun third-party *app* password, not the login password), `url`, `sync_source_folder` and
  `sync_dest_folder` (`/Kindle` → `/mnt/us/books`). It gets written from `.env` over USB rather than
  typed on the Kindle keyboard.
- **The inbox is now backend-agnostic**: `send_to_inbox` / `list_inbox`, with `KINDLE_INBOX` in
  `.env` choosing `webdav` (default) or `dropbox`. Changing cloud provider again will not touch code.

**Concern:** The Dropbox refresh token sat in plain text on the Kindle's USB-visible storage — any
computer that Kindle is plugged into could read it. It has been **deleted from the device** (the
whole `koreader/` tree was scanned to confirm), and the only remaining copy is in `.env`. The same
token was also pasted into a chat session, so it should be rotated regardless. The Jianguoyun app
password will land in the same plain-text position on the device, which is the accepted trade-off
for not typing it on an e-ink keyboard; an app password is revocable and scoped, unlike the account
password. Jianguoyun's free tier also has monthly upload/download quotas, and a ~300 MB shelf is not
nothing.

**Status:** Unfinished. `WEBDAV_USERNAME` and `WEBDAV_PASSWORD` in `.env` are still **empty** —
the Jianguoyun account has not been created — so `list_inbox` and `send_to_inbox` currently fail
with "WebDAV is not configured", by design rather than by accident.

### Session 4 (22:49–22:51) — first commit and push

**What:** Put the project under git and pushed to
`github.com/bluehawana/kindle-koreader-mcp-dropbox` (public). Five files went up: `README.md`,
`kindle_mcp/server.py`, `scripts/copy_winterbreak.sh`, `.gitignore`, `.env.example`. Corrected a
stale README claim while there: updates are blocked by the jailbreak's `jb.sh` now, not by the
storage filler, which was deleted right after the jailbreak.

**Decisions — what stays out of a public repo:**
- `.env` — Dropbox credentials, and soon the WebDAV app password.
- `backup/` — files pulled off the Kindle; the WinterBreak payload's `LocalStorage` carries Amazon
  account identifiers.
- `lists/` — a personal library, and some entry names still carry the original buyer's name stamped
  into O'Reilly early-release ePubs. Someone else's name is not ours to publish.

### Session 5 (23:39–23:55) — agent docs, and this diary

**What:** Added `CLAUDE.md` (project brief: what the server is, where the reference lives, where
secrets live) and `docs/agents/` — `issue-tracker.md` (GitHub issues via `gh`), `triage-labels.md`
(the five canonical triage labels), `domain.md` (how to consume `GLOSSARY.md` / `docs/adr/` when
they exist; neither does yet, which is fine). Then wrote this diary, which the first push should
have included and did not.

### Not verified

- **End-to-end wireless delivery has never run.** No Jianguoyun account, so no WebDAV credentials,
  so nothing has synced to the Kindle over the air.
- The 25 uploaded books are in **Dropbox** `/Kindle`, which this Kindle cannot reach. They will have
  to be re-uploaded to the WebDAV inbox; the Dropbox copy is not a backup of anything that works.
- The Kindle is **not connected right now** — `kindle_status` reports no device — so nothing about
  the device's current on-disk state was re-checked during this close-out.
- SimpleUI 3.0.0 has not been exercised: it is installed-but-disabled.
- The `dropbox` inbox backend has not been re-tested since the refactor into `send_to_inbox`.

### Open

1. Register Jianguoyun, generate a **third-party app password**, fill `WEBDAV_USERNAME` /
   `WEBDAV_PASSWORD` in `.env`.
2. Write the WebDAV entry into `cloudstorage.lua` over USB, then test one book end to end:
   `send_to_inbox` → KOReader → Tools → Cloud storage+ → long-press → *Sync now*.
3. Re-upload the `lists/llm.txt` shelf to the WebDAV inbox, watching the free-tier quota.
4. **Rotate the Dropbox refresh token** — it was on the Kindle in plain text and pasted into a chat.
5. Install the Jianguoyun iOS app if the phone should send books without the Mac being awake.
6. Decide whether to re-enable SimpleUI, after the reading path has been boring for a while.
7. **Never let this Kindle take a firmware update.** 5.18.1 and later have no public jailbreak.
