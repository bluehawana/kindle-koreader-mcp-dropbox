#!/usr/bin/env python3
"""Kindle MCP server.

Moves ePub/PDF/MOBI files onto a USB-mounted Kindle running KOReader.
No format conversion is performed or needed: KOReader reads ePub natively.

Zero third-party dependencies. Speaks MCP over stdio (newline-delimited JSON-RPC 2.0).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from the project's .env without overriding the real environment."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv(PROJECT_ROOT / ".env")

# Books live outside `documents/` so Kindle's native firmware does not try to
# index formats it cannot open; KOReader browses this folder directly.
BOOKS_SUBDIR = os.environ.get("KINDLE_BOOKS_SUBDIR", "books")
SOURCE_DIRS = [
    Path(p).expanduser()
    for p in os.environ.get(
        "KINDLE_SOURCE_DIRS",
        # Apple Books keeps its iCloud library here; anything opened in Books on
        # an iPhone or iPad syncs down to this folder on the Mac.
        "~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents"
        ":~/Downloads:~/Desktop:~/Documents",
    ).split(":")
    if p.strip()
]
EBOOK_SUFFIXES = {".epub", ".pdf", ".mobi", ".azw3", ".azw", ".cbz", ".cbr", ".fb2", ".djvu", ".txt"}

# Z-Library and similar sources append provenance junk to filenames.
JUNK_PATTERNS = [
    r"\(?\s*z-?lib(?:rary)?(?:\.org)?\s*\)?",
    r"_+z_+lib_+org_*",
    r"_+Z_+Library_*",
    r"\(\s*Z-Library\s*\)",
    r"-cdeKey_[A-Z0-9]+",
    r"_B0[0-9A-Z]{8}",
    r"\(it-ebooks\)",
    # O'Reilly early-release copies are stamped with the buyer's name.
    r"\(\s*for\s+[^()]*\)",
    r"\s+for\s*$",
]


# --------------------------------------------------------------------------
# Kindle discovery
# --------------------------------------------------------------------------

def find_kindle() -> Path | None:
    """Return the mount point of a connected Kindle, or None."""
    explicit = os.environ.get("KINDLE_MOUNT")
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None
    for vol in sorted(Path("/Volumes").glob("*")):
        try:
            if (vol / "system" / "version.txt").is_file():
                return vol
        except OSError:
            continue
    return None


def kindle_firmware(mount: Path) -> str | None:
    try:
        return (mount / "system" / "version.txt").read_text(errors="replace").strip()
    except OSError:
        return None


def free_mb(mount: Path) -> int:
    st = os.statvfs(mount)
    return (st.f_bavail * st.f_frsize) // (1024 * 1024)


# --------------------------------------------------------------------------
# Filename / metadata handling
# --------------------------------------------------------------------------

def is_ebook(path: Path) -> bool:
    """True for ebook files, and for the unzipped .epub folders Apple Books keeps."""
    if path.name.startswith("._") or path.suffix.lower() not in EBOOK_SUFFIXES:
        return False
    if path.is_dir():
        return path.suffix.lower() == ".epub" and (path / "META-INF" / "container.xml").is_file()
    return path.is_file()


def ebook_size(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size


# Apple Books adds these to its unzipped ePubs; they are not part of the book.
EPUB_DIR_SKIP = {"iTunesMetadata.plist", "iTunesMetadata-original.plist", ".DS_Store"}


def zip_epub_dir(src: Path, dest: Path) -> None:
    """Pack an unzipped ePub folder into a valid .epub file.

    The OCF spec requires `mimetype` to be the first entry, stored uncompressed;
    readers sniff for it at a fixed offset.
    """
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        mimetype = src / "mimetype"
        data = mimetype.read_bytes() if mimetype.is_file() else b"application/epub+zip"
        zf.writestr(zipfile.ZipInfo("mimetype"), data, compress_type=zipfile.ZIP_STORED)
        for f in sorted(src.rglob("*")):
            if not f.is_file() or f.name in EPUB_DIR_SKIP or f.name.startswith("._"):
                continue
            rel = f.relative_to(src).as_posix()
            if rel != "mimetype":
                zf.write(f, rel)


def epub_metadata(path: Path) -> tuple[str | None, str | None]:
    """Extract (title, author) from an ePub's OPF package document."""
    if path.is_dir():
        read = lambda name: (path / name).read_bytes()
        return _opf_metadata(read)
    try:
        with zipfile.ZipFile(path) as zf:
            return _opf_metadata(zf.read)
    except (zipfile.BadZipFile, OSError):
        return None, None


def _opf_metadata(read) -> tuple[str | None, str | None]:
    ns = {
        "c": "urn:oasis:names:tc:opendocument:xmlns:container",
        "opf": "http://www.idpf.org/2007/opf",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    try:
        container = ElementTree.fromstring(read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", ns)
        if rootfile is None:
            return None, None
        opf_path = rootfile.get("full-path")
        if not opf_path:
            return None, None
        opf = ElementTree.fromstring(read(opf_path))
        title_el = opf.find(".//dc:title", ns)
        author_el = opf.find(".//dc:creator", ns)
        title = (title_el.text or "").strip() if title_el is not None else None
        author = (author_el.text or "").strip() if author_el is not None else None
        return (title or None), (author or None)
    except (KeyError, ElementTree.ParseError, OSError):
        return None, None


def strip_junk(name: str) -> str:
    for pat in JUNK_PATTERNS:
        name = re.sub(pat, " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[_\s]+", " ", name)
    # Source filenames often read "Title.pdf-cdeKey_XXX.pdf"; once the cdeKey is
    # gone a bare extension can be left stranded inside the stem.
    name = re.sub(
        r"\.(?:epub|pdf|mobi|azw3?|cbz|cbr|fb2|djvu|txt)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    # Truncated listings leave a trailing ellipsis behind.
    name = re.sub(r"\s*\.{2,}\s*$", "", name)
    return name.strip(" -_.")


# Metadata fields are frequently filled with placeholders or the authoring
# tool's account name; these must never become part of a filename.
JUNK_META = {
    "", "[no data]", "no data", "unknown", "unknown author", "untitled",
    "no title", "none", "n/a", "null", "administrator", "admin", "user",
    "calibre", "anonymous", "author", "default", "epub", "document",
    "author name", "author names", "author names here",
    "未知", "未知作者", "佚名", "无", "作者", "用户",
}


def clean_meta(value: str | None) -> str | None:
    """Return a usable metadata value, or None if it is a placeholder."""
    if not value:
        return None
    value = re.sub(r"<[^>]*>", "", value)  # stray markup such as <b>Name</b>
    value = strip_junk(value)
    if not value or value.strip().lower() in JUNK_META:
        return None
    # Broken conversions leave a content filename (chapter-1.html) in the field.
    if re.search(r"\.(?:x?html?|xml|opf|ncx)$", value, flags=re.IGNORECASE):
        return None
    return value


def safe_filename(name: str, suffix: str) -> str:
    """Produce a FAT32-safe filename, preserving CJK characters."""
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[:/\\*?"<>|\x00-\x1f]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # FAT32 long filenames cap at 255 UTF-16 units; stay well clear.
    max_stem = 120
    if len(name) > max_stem:
        name = name[:max_stem].rstrip(" .-")
    return (name or "untitled") + suffix


def target_name(src: Path) -> str:
    """Choose a clean destination filename for a source ebook."""
    suffix = src.suffix.lower()
    title = author = None
    if suffix == ".epub":
        raw_title, raw_author = epub_metadata(src)
        title, author = clean_meta(raw_title), clean_meta(raw_author)

    from_name = strip_junk(src.stem)

    # Metadata titles are sometimes a bare series or author name, while the
    # filename carries the full book title. Prefer the filename when it already
    # contains the metadata title and genuinely adds to it.
    # The one exception is a trailing "for <buyer>" stamp, which is exactly what
    # strip_junk removed from the metadata title.
    if (title and from_name and title in from_name and len(from_name) > len(title) + 2
            and not re.match(
                re.escape(title) + r"\s+for\s", from_name)):
        title = from_name

    if title and author and author.lower() not in title.lower():
        stem = f"{title} - {author}"
    else:
        stem = title or from_name or src.stem
    return safe_filename(stem, suffix)


def unique_path(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(filename)
    for i in range(2, 100):
        candidate = dest_dir / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free name for {filename}")


# --------------------------------------------------------------------------
# Dropbox (the wireless inbox KOReader pulls from)
# --------------------------------------------------------------------------

DROPBOX_FOLDER = os.environ.get("DROPBOX_KINDLE_FOLDER", "/Kindle")
# Upload in chunks: one long request for a 20+ MB book drops easily on flaky
# Wi-Fi, and a failed chunk is cheap to retry. Sessions also lift the 150 MB cap.
DROPBOX_CHUNK = 8 * 1024 * 1024
_dropbox_token: tuple[str, float] | None = None


def dropbox_token() -> str:
    """Exchange the long-lived refresh token for a short-lived access token."""
    global _dropbox_token
    if _dropbox_token and _dropbox_token[1] > time.time() + 60:
        return _dropbox_token[0]
    key = os.environ.get("DROPBOX_APP_KEY")
    secret = os.environ.get("DROPBOX_APP_SECRET")
    refresh = os.environ.get("DROPBOX_REFRESH_TOKEN")
    if not (key and secret and refresh):
        raise RuntimeError(f"Dropbox is not configured: fill in {PROJECT_ROOT / '.env'}")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": key,
        "client_secret": secret,
    }).encode()
    with urllib.request.urlopen("https://api.dropboxapi.com/oauth2/token", body, timeout=30) as r:
        data = json.load(r)
    _dropbox_token = (data["access_token"], time.time() + int(data.get("expires_in", 14400)))
    return _dropbox_token[0]


def dropbox_call(endpoint: str, args: dict, data: bytes | None = None) -> dict:
    """Call a Dropbox API v2 endpoint. `data` switches to a content-upload request."""
    headers = {"Authorization": f"Bearer {dropbox_token()}"}
    if data is None:
        url = f"https://api.dropboxapi.com/2/{endpoint}"
        headers["Content-Type"] = "application/json"
        payload = json.dumps(args).encode()
    else:
        url = f"https://content.dropboxapi.com/2/{endpoint}"
        headers["Content-Type"] = "application/octet-stream"
        # HTTP headers must be ASCII; json.dumps escapes CJK filenames as \uXXXX.
        headers["Dropbox-API-Arg"] = json.dumps(args)
        payload = data
    req = urllib.request.Request(url, payload, headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error_summary", detail)
        except ValueError:
            pass
        raise RuntimeError(detail) from None


def dropbox_retry(endpoint: str, args: dict, data: bytes | None = None, attempts: int = 4) -> dict:
    for i in range(attempts):
        try:
            return dropbox_call(endpoint, args, data)
        except (urllib.error.URLError, OSError) as e:
            # RuntimeError carries an API error (e.g. a name conflict): never retry it.
            if isinstance(e, RuntimeError) or i == attempts - 1:
                raise
            time.sleep(2 ** i)
    raise AssertionError("unreachable")


def dropbox_upload(path: str, data: bytes) -> dict:
    """Upload `data` to `path` through an upload session, chunk by chunk."""
    commit = {"path": path, "mode": "add", "autorename": False, "mute": True}
    if len(data) <= DROPBOX_CHUNK:
        return dropbox_retry("files/upload", commit, data)
    session = dropbox_retry("files/upload_session/start", {"close": False}, data[:DROPBOX_CHUNK])
    offset = DROPBOX_CHUNK
    while len(data) - offset > DROPBOX_CHUNK:
        dropbox_retry("files/upload_session/append_v2", {
            "cursor": {"session_id": session["session_id"], "offset": offset},
            "close": False,
        }, data[offset:offset + DROPBOX_CHUNK])
        offset += DROPBOX_CHUNK
    return dropbox_retry("files/upload_session/finish", {
        "cursor": {"session_id": session["session_id"], "offset": offset},
        "commit": commit,
    }, data[offset:])


# --------------------------------------------------------------------------
# WebDAV (Jianguoyun): the inbox the Kindle can actually reach. Dropbox is
# DNS-poisoned on the Kindle's network and the Kindle cannot use a proxy.
# --------------------------------------------------------------------------

WEBDAV_FOLDER = os.environ.get("WEBDAV_KINDLE_FOLDER", "/Kindle")


def webdav_request(method: str, path: str, data: bytes | None = None,
                   headers: dict | None = None) -> tuple[int, bytes]:
    """Send a WebDAV request; returns (status, body) and never raises on HTTP errors."""
    base = os.environ.get("WEBDAV_URL", "").rstrip("/")
    user = os.environ.get("WEBDAV_USERNAME")
    password = os.environ.get("WEBDAV_PASSWORD")
    if not (base and user and password):
        raise RuntimeError(f"WebDAV is not configured: fill in {PROJECT_ROOT / '.env'}")
    url = base + urllib.parse.quote(path)
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(url, data, {"Authorization": f"Basic {auth}", **(headers or {})},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def webdav_list(folder: str) -> list[dict]:
    """PROPFIND one level deep; returns the files (not folders) in `folder`."""
    status, body = webdav_request("PROPFIND", folder.rstrip("/") + "/", b"", {"Depth": "1"})
    if status == 404:
        return []
    if status != 207:
        raise RuntimeError(f"PROPFIND {folder} -> HTTP {status}")
    ns = {"d": "DAV:"}
    files = []
    for resp in ElementTree.fromstring(body).findall("d:response", ns):
        if resp.find(".//d:resourcetype/d:collection", ns) is not None:
            continue
        href = urllib.parse.unquote(resp.findtext("d:href", "", ns))
        files.append({
            "name": href.rstrip("/").rsplit("/", 1)[-1],
            "size": int(resp.findtext(".//d:getcontentlength", "0", ns) or 0),
            "modified": resp.findtext(".//d:getlastmodified", "", ns),
        })
    return files


def webdav_upload(folder: str, name: str, data: bytes) -> str:
    """PUT a book into `folder`. Returns "OK" or "HAVE" (same name and size already there)."""
    existing = {f["name"]: f["size"] for f in webdav_list(folder)}
    if not existing:
        status, _ = webdav_request("MKCOL", folder.rstrip("/") + "/")
        if status not in (201, 405):  # 405: it already exists
            raise RuntimeError(f"MKCOL {folder} -> HTTP {status}")
    if existing.get(name) == len(data):
        return "HAVE"
    for i in range(4):
        try:
            status, body = webdav_request("PUT", f"{folder.rstrip('/')}/{name}", data,
                                          {"Content-Type": "application/epub+zip"})
            break
        except (urllib.error.URLError, OSError):
            if i == 3:
                raise
            time.sleep(2 ** i)
    if status not in (200, 201, 204):
        raise RuntimeError(f"PUT -> HTTP {status} {body[:200]!r}")
    return "OK"


def ebook_bytes(src: Path) -> bytes:
    """The book as a single file's bytes, packing Apple Books .epub folders on the way."""
    if not src.is_dir():
        return src.read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        packed = Path(tmp) / "book.epub"
        zip_epub_dir(src, packed)
        return packed.read_bytes()


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

MAX_SCAN_DEPTH = int(os.environ.get("KINDLE_SCAN_DEPTH", "3"))


def scan_ebooks(root: Path, pattern: str = "") -> list[Path]:
    """Walk `root` up to MAX_SCAN_DEPTH levels, collecting ebook files.

    Depth-bounded so a large ~/Documents tree cannot stall a tool call.
    """
    results: list[Path] = []
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        here = Path(dirpath)
        if len(here.parts) - root_depth >= MAX_SCAN_DEPTH:
            dirnames[:] = []
        # An unzipped .epub folder is a book in its own right; never descend into it.
        epub_dirs = [d for d in dirnames if d.lower().endswith(".epub")]
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in epub_dirs]
        for fn in filenames + epub_dirs:
            if pattern and pattern not in fn.lower():
                continue
            if is_ebook(here / fn):
                results.append(here / fn)
    return results


def tool_status(_args: dict) -> str:
    mount = find_kindle()
    if mount is None:
        return (
            "No Kindle detected.\n"
            "Connect it by USB and make sure it is mounted (it should appear in /Volumes).\n"
            "Note: 11th-gen and newer Kindles use MTP and will not mount this way."
        )
    books_dir = mount / BOOKS_SUBDIR
    lines = [
        f"Kindle mounted at: {mount}",
        f"Firmware: {kindle_firmware(mount) or 'unknown'}",
        f"Free space: {free_mb(mount)} MB",
        f"KOReader installed: {'yes' if (mount / 'koreader').is_dir() else 'no'}",
        f"Books folder: {books_dir} ({'exists' if books_dir.is_dir() else 'will be created on first send'})",
    ]
    plugins = mount / "koreader" / "plugins"
    if plugins.is_dir():
        installed = sorted(p.name for p in plugins.glob("*.koplugin"))
        lines.append(f"KOReader plugins: {', '.join(installed) if installed else '(none)'}")
    fillers = mount / "fill_disk"
    if fillers.is_dir():
        lines.append("NOTE: fill_disk/ is still present (OTA-update filler). Safe to delete after jailbreaking.")
    return "\n".join(lines)


def tool_find(args: dict) -> str:
    limit = int(args.get("limit", 20))
    pattern = (args.get("pattern") or "").lower()
    found: list[Path] = []
    for d in SOURCE_DIRS:
        if not d.is_dir():
            continue
        found.extend(scan_ebooks(d, pattern))
    if not found:
        where = ", ".join(str(d) for d in SOURCE_DIRS)
        return f"No ebook files found in: {where}"
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = [f"Found {len(found)} ebook(s); newest first:"]
    for p in found[:limit]:
        size = ebook_size(p) // 1024
        out.append(f"  {p}  [{size} KB]  ->  would land as: {target_name(p)}")
    return "\n".join(out)


def tool_send(args: dict) -> str:
    paths_arg = args.get("paths") or []
    if isinstance(paths_arg, str):
        paths_arg = [paths_arg]
    if not paths_arg:
        return "Error: provide `paths` (one or more ebook file paths)."

    mount = find_kindle()
    if mount is None:
        return "Error: no Kindle detected. Connect it by USB first, then retry."

    dest_dir = mount / BOOKS_SUBDIR
    rename = bool(args.get("rename", True))
    results: list[str] = []
    copied = 0

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"Error: cannot create {dest_dir}: {e}"

    for raw in paths_arg:
        src = Path(str(raw)).expanduser()
        if not src.exists():
            results.append(f"  SKIP  {src}  (not found)")
            continue
        if not is_ebook(src):
            results.append(f"  SKIP  {src.name}  (not an ebook)")
            continue
        size_mb = ebook_size(src) / (1024 * 1024)
        if size_mb > free_mb(mount):
            results.append(f"  FAIL  {src.name}  (needs {size_mb:.1f} MB, only {free_mb(mount)} MB free)")
            continue
        filename = target_name(src) if rename else safe_filename(src.stem, src.suffix.lower())
        dest = unique_path(dest_dir, filename)
        try:
            # copyfile, not copy2: copy2 carries macOS xattrs across, which FAT32
            # cannot store natively and so materialise as a ._ AppleDouble sidecar
            # sitting next to every book. Leaving mtime at "now" is also what we
            # want, since SimpleUI sorts the shelf by file date, newest first.
            if src.is_dir():
                zip_epub_dir(src, dest)
            else:
                shutil.copyfile(src, dest)
            copied += 1
            results.append(f"  OK    {src.name}\n          -> {dest.name}")
        except OSError as e:
            dest.unlink(missing_ok=True)  # never leave a half-written book behind
            results.append(f"  FAIL  {src.name}  ({e})")
        # Clear any sidecar the filesystem produced regardless.
        sidecar = dest.parent / ("._" + dest.name)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass

    header = f"Copied {copied}/{len(paths_arg)} file(s) to {dest_dir}"
    footer = ""
    if copied:
        footer = (
            "\n\nEject the Kindle, then open KOReader and browse to "
            f"/mnt/us/{BOOKS_SUBDIR} to read. No conversion was needed."
        )
    return header + "\n" + "\n".join(results) + footer


INBOX = os.environ.get("KINDLE_INBOX", "webdav").lower()


def tool_upload(args: dict) -> str:
    paths_arg = args.get("paths") or []
    if isinstance(paths_arg, str):
        paths_arg = [paths_arg]
    if not paths_arg:
        return "Error: provide `paths` (one or more ebook paths)."
    if INBOX == "webdav":
        return _upload_webdav(paths_arg, args.get("folder"))
    folder = "/" + (args.get("folder") or DROPBOX_FOLDER).strip("/")
    results: list[str] = []
    uploaded = 0
    for raw in paths_arg:
        src = Path(str(raw)).expanduser()
        if not src.exists() or not is_ebook(src):
            results.append(f"  SKIP  {src.name}  (not an ebook)")
            continue
        name = target_name(src)
        try:
            data = ebook_bytes(src)
            # mode=add without autorename: a name clash means the book is already in the inbox.
            dropbox_upload(f"{folder}/{name}", data)
            uploaded += 1
            results.append(f"  OK    {name}  [{len(data) // 1024} KB]")
        except RuntimeError as e:
            if "conflict" in str(e):
                results.append(f"  HAVE  {name}  (already in {folder})")
            else:
                results.append(f"  FAIL  {src.name}  ({e})")
        except (urllib.error.URLError, OSError) as e:
            # Network trouble on one book must not abort the rest of the batch.
            results.append(f"  FAIL  {src.name}  (network: {e})")
    header = f"Uploaded {uploaded}/{len(paths_arg)} book(s) to Dropbox {folder}"
    footer = ("\n\nOn the Kindle: KOReader -> Cloud storage -> Dropbox -> "
              f"{folder}, then download or sync.") if uploaded else ""
    return header + "\n" + "\n".join(results) + footer


def _upload_webdav(paths_arg: list, folder_arg: str | None) -> str:
    folder = "/" + (folder_arg or WEBDAV_FOLDER).strip("/")
    results: list[str] = []
    uploaded = 0
    for raw in paths_arg:
        src = Path(str(raw)).expanduser()
        if not src.exists() or not is_ebook(src):
            results.append(f"  SKIP  {src.name}  (not an ebook)")
            continue
        name = target_name(src)
        try:
            data = ebook_bytes(src)
            outcome = webdav_upload(folder, name, data)
            if outcome == "OK":
                uploaded += 1
            results.append(f"  {outcome:<4}  {name}  [{len(data) // 1024} KB]")
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            results.append(f"  FAIL  {src.name}  ({e})")
    header = f"Uploaded {uploaded}/{len(paths_arg)} book(s) to WebDAV {folder}"
    footer = ("\n\nOn the Kindle: KOReader -> Tools -> Cloud storage+ -> long-press the "
              "server -> Sync now.") if uploaded else ""
    return header + "\n" + "\n".join(results) + footer


def tool_dropbox_list(args: dict) -> str:
    if INBOX == "webdav":
        folder = "/" + (args.get("folder") or WEBDAV_FOLDER).strip("/")
        files = sorted(webdav_list(folder), key=lambda f: f["name"])
        out = [f"WebDAV {folder}: {len(files)} file(s)"]
        out += [f"  {f['name']}  [{f['size'] // 1024} KB]" for f in files]
        return "\n".join(out)
    folder = "/" + (args.get("folder") or DROPBOX_FOLDER).strip("/")
    resp = dropbox_call("files/list_folder", {"path": folder})
    entries = resp["entries"]
    while resp.get("has_more"):
        resp = dropbox_call("files/list_folder/continue", {"cursor": resp["cursor"]})
        entries += resp["entries"]
    files = sorted((e for e in entries if e[".tag"] == "file"),
                   key=lambda e: e.get("server_modified", ""), reverse=True)
    out = [f"Dropbox {folder}: {len(files)} file(s), newest first"]
    for e in files:
        out.append(f"  {e['name']}  [{e['size'] // 1024} KB, {e['server_modified'][:10]}]")
    return "\n".join(out)


def tool_list(args: dict) -> str:
    mount = find_kindle()
    if mount is None:
        return "Error: no Kindle detected."
    include_docs = bool(args.get("include_documents", False))
    dirs = [mount / BOOKS_SUBDIR]
    if include_docs:
        dirs.append(mount / "documents")
    out: list[str] = []
    for d in dirs:
        if not d.is_dir():
            out.append(f"{d.name}/: (does not exist)")
            continue
        files = sorted(
            (
                p for p in d.rglob("*")
                if p.is_file()
                and not p.name.startswith("._")
                and p.suffix.lower() in EBOOK_SUFFIXES
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out.append(f"{d.name}/: {len(files)} book(s)")
        for p in files:
            out.append(f"  {p.relative_to(d)}  [{p.stat().st_size // 1024} KB]")
    return "\n".join(out)


TOOLS = [
    {
        "name": "kindle_status",
        "description": (
            "Check whether a Kindle is connected over USB and report its mount point, "
            "firmware version, free space, whether KOReader and its plugins are installed, "
            "and where books are stored."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_status,
    },
    {
        "name": "find_ebooks",
        "description": (
            "Search the configured source folders for ebooks, newest first. By default this covers "
            "the Apple Books iCloud library (books opened in Books on iPhone/iPad sync here), "
            "plus ~/Downloads, ~/Desktop and ~/Documents. Use it to locate a book downloaded "
            "from a Telegram bot before sending it to the Kindle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Optional case-insensitive substring to match against filenames.",
                },
                "limit": {"type": "integer", "description": "Maximum results to return (default 20)."},
            },
        },
        "handler": tool_find,
    },
    {
        "name": "send_to_kindle",
        "description": (
            "Copy one or more ebook files to the connected Kindle for reading in KOReader. "
            "ePub is copied as-is with no conversion. Filenames are cleaned up and renamed from "
            "embedded ePub metadata (Title - Author) unless rename is false."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of the ebooks to copy (files, or Apple Books .epub folders).",
                },
                "rename": {
                    "type": "boolean",
                    "description": "Rename using ePub metadata and strip source junk (default true).",
                },
            },
            "required": ["paths"],
        },
        "handler": tool_send,
    },
    {
        "name": "list_kindle_books",
        "description": "List the ebooks currently stored on the connected Kindle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_documents": {
                    "type": "boolean",
                    "description": "Also list Kindle's native documents/ folder (default false).",
                }
            },
        },
        "handler": tool_list,
    },
]

TOOLS += [
    {
        "name": "send_to_inbox",
        "description": (
            "Wireless delivery: upload ebooks to the cloud inbox folder (default /Kindle) that "
            "KOReader on the Kindle syncs from over Wi-Fi. The backend is KINDLE_INBOX in .env: "
            "webdav (Jianguoyun, reachable from the Kindle's network) or dropbox. No USB needed, "
            "and the Kindle can be asleep. Accepts files or Apple Books .epub folders; filenames "
            "are cleaned from ePub metadata. Prefer this over send_to_kindle unless the Kindle is "
            "plugged in."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of the ebooks (files, or Apple Books .epub folders).",
                },
                "folder": {"type": "string", "description": "Inbox folder (default /Kindle)."},
            },
            "required": ["paths"],
        },
        "handler": tool_upload,
    },
    {
        "name": "list_inbox",
        "description": "List the books waiting in the cloud inbox folder KOReader syncs from.",
        "inputSchema": {
            "type": "object",
            "properties": {"folder": {"type": "string", "description": "Inbox folder (default /Kindle)."}},
        },
        "handler": tool_dropbox_list,
    },
]

HANDLERS = {t["name"]: t["handler"] for t in TOOLS}
TOOL_SCHEMAS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def respond(msg_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req: dict) -> None:
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        respond(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "kindle-mcp", "version": "1.2.0"},
        })
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOL_SCHEMAS})
    elif method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            respond(msg_id, error={"code": -32602, "message": f"Unknown tool: {name}"})
            return
        try:
            text = handler(params.get("arguments") or {})
            respond(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as e:  # surface errors to the model, don't crash the server
            respond(msg_id, {
                "content": [{"type": "text", "text": f"Tool {name} failed: {e!r}"}],
                "isError": True,
            })
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass  # notifications carry no id and need no reply
    elif msg_id is not None:
        respond(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception as e:
            print(f"kindle-mcp internal error: {e!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
