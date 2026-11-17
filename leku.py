#!/usr/bin/env python
"""leku — drive lekuluent.et's underlying services from the CLI.

lekuluent.et is an Ethiopian movie/TV streaming site. Despite looking like a
custom app, it is a thin Next.js frontend over two public services:

  - TMDB                     search + metadata (key is shipped in the site JS)
  - public embed providers   playback, keyed by TMDB id (vidsrc family)

This CLI reproduces what the site's own player does, so you can search, get
playable stream URLs, and download from a terminal.

Usage:
  python leku.py search "batman"
  python leku.py search "the office" --type tv
  python leku.py stream 155                 # movie embed URLs (open in browser)
  python leku.py stream 1399 -s 1 -e 1     # tv episode embed URLs
  python leku.py download 155 --type movie  # movie -> ./downloads
  python leku.py download 1399 -s 1 -e 1    # single tv episode
  python leku.py download 1399 -s 1         # whole season
  python leku.py download 1399 --type tv     # every episode of the show
  python leku.py download 155 --type movie --fragments 20   # quick sample

NOTE: TMDB movie and tv ids are SEPARATE namespaces and often collide
(movie 155 = The Dark Knight, tv 155 = 3rd Rock from the Sun), so a bare
id that exists in both needs --type movie|tv. The `search` output shows the
type next to every id.

Requires: node (wasm stream-url decryption), yt-dlp (HLS download).
"""
import argparse, json, os, pathlib, re, subprocess, sys, tempfile, time
import urllib.parse, urllib.request

TMDB = "https://api.themoviedb.org/3"
# Public key shipped in lekuluent.et's client JS (Next.js chunk 383).
TMDB_KEY = "0c43ea3d59eb841f28252be73f56fb09"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36"
VIDSRC_ORIGIN = "https://vidsrc.sh"

# Stream providers, exactly as lekuluent.et builds them (lazy chunks 4847/8312).
# {i}=tmdb id, {s}=season, {e}=episode
MOVIE = [
    "https://vidsrc.sh/embed/movie/{i}",
    "https://vidlink.pro/movie/{i}",
    "https://vidfast.vc/movie/{i}",
    "https://vidrock.to/movie/{i}",
    "https://embed.vidsrc.si/embed/movie/{i}?autoplay=1&ds_lang=en",
]
TV = [
    "https://vidsrc.sh/embed/tv/{i}/{s}/{e}",
    "https://vidlink.pro/tv/{i}/{s}/{e}",
    "https://vidfast.vc/tv/{i}/{s}/{e}?autoplay=true",
    "https://vidrock.to/tv/{i}/{s}/{e}",
    "https://embed.vidsrc.si/embed/tv/{i}/{s}/{e}?autoplay=1",
]

# Embedded port of the provider's own vsdec.js: decrypts the encrypted
# `stream_urls` envelope (base64 ChaCha20 nonce||ciphertext) using the
# per-5-minute-window WASM whose URL the API hands out. Written to a temp
# file at runtime and run under node; the streams JSON goes in on stdin.
VSDENC_JS = """\
import { readFileSync } from "node:fs";
const j = JSON.parse(readFileSync(0, "utf8"));
if (Array.isArray(j.data.stream_urls)) {
  console.log(JSON.stringify(j.data.stream_urls)); process.exit(0);
}
if (typeof j.data.stream_urls !== "string" || !j.vs) {
  console.error("no encrypted stream_urls / vs envelope found"); process.exit(1);
}
const res = await fetch(j.vs.wasm_url, { headers: { accept: "application/octet-stream" } });
if (!res.ok) { console.error(`wasm fetch failed: ${res.status}`); process.exit(1); }
const bytes = new Uint8Array(await res.arrayBuffer());
const inst = await WebAssembly.instantiate(await WebAssembly.compile(bytes), {});
const ex = inst.exports;
const enc = Uint8Array.from(atob(j.data.stream_urls), c => c.charCodeAt(0));
const ptr = ex.alloc(enc.length);
new Uint8Array(ex.memory.buffer, ptr, enc.length).set(enc);
const outLen = ex.decrypt(ptr, enc.length);
const out = new TextDecoder().decode(new Uint8Array(ex.memory.buffer, ptr + 12, outLen));
console.log(JSON.stringify(out.split("\\n").filter(Boolean)));
"""


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _tv_season_map(tid):
    """[(season_number, episode_count)] for every real season (TMDB)."""
    q = urllib.parse.urlencode({"api_key": TMDB_KEY})
    data = _get(f"{TMDB}/tv/{tid}?{q}")
    return [(s["season_number"], s.get("episode_count") or 0)
            for s in data.get("seasons", [])
            if (s.get("season_number") or 0) >= 1]


def _tmdb_kind(tid):
    """Which TMDB namespace a bare id belongs to: 'movie', 'tv', or exits
    with an error. Ids are separate per namespace, so e.g. movie 155 (The
    Dark Knight) and tv 155 (3rd Rock from the Sun) both exist."""
    q = urllib.parse.urlencode({"api_key": TMDB_KEY})
    found = []
    for kind in ("movie", "tv"):
        try:
            _get(f"{TMDB}/{kind}/{tid}?{q}")
            found.append(kind)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if not found:
        sys.exit(f"error: tmdb id {tid} not found as a movie or tv show")
    if len(found) == 2:
        sys.exit(f"error: tmdb id {tid} exists as BOTH a movie and a tv show "
                 f"(ids are per-namespace); pass --type movie or --type tv")
    return found[0]


def search(query, mtype="multi"):
    q = urllib.parse.urlencode({"api_key": TMDB_KEY, "query": query})
    data = _get(f"{TMDB}/search/{mtype}?{q}")
    for r in data.get("results", []):
        t = r.get("media_type", mtype if mtype != "multi" else "?")
        name = r.get("title") or r.get("name") or "?"
        date = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        if t == "person":
            continue
        # poster on TMDB's public image CDN (same one the site uses)
        poster = r.get("poster_path")
        purl = f"https://image.tmdb.org/t/p/w342{poster}" if poster else "-"
        print(f"{r['id']:>8}  {t:<5}  {date:<4}  {name}  {purl}")


def stream(tid, season, episode):
    tmpl = TV if (season and episode) else MOVIE
    for u in tmpl:
        print(u.format(i=tid, s=season, e=episode))


# ---------------------------------------------------------------------------
# download: resolve the vidsrc-family chain end to end, then hand yt-dlp a
# local, token-stamped HLS playlist.
#
#   1. GET  {origin}/vs_src.php?type=..&id=..[&season=..&episode=..]
#        -> JSON {src: landing URL with signed ?vs= token}
#   2. GET  landing URL -> HTML with CFG.playerUrl (nested player iframe)
#   3. GET  landing+playerUrl -> HTML with window.CONFIG {
#        api (movies) / streamBase (tv), metaApi, apiToken (single-use) }
#   4. GET  metaApi -> JSON advertising stream_urls_api_host (rotating pool
#        host the stream_urls request must go to)
#   5. GET  pool_host/api.php?...&stream_urls&api_token=<single-use>
#        -> JSON with encrypted data.stream_urls + vs.wasm_url
#   6. node (embedded vsdec.js port) -> [playlist URLs]
#   7. GET  pool_host/generate.php?api_token= -> JWT (IP+range bound, ~4h)
#   8. GET  playlist?token=JWT -> master m3u8 -> variant m3u8
#        (every request needs ?token= AND Referer: <landing origin>)
#   9. rewrite segment URLs absolute + token -> local m3u8 -> yt-dlp
# ---------------------------------------------------------------------------

class GatedError(RuntimeError):
    """The provider served a captcha-gated player page (no apiToken)."""

    def __init__(self, reason, embed):
        self.reason = reason
        self.embed = embed
        super().__init__(f"captcha-gated (reason: {reason}) for {embed}")


def _gate_help(err):
    """Print the human-friendly captcha recovery instructions."""
    print(file=sys.stderr)
    print("You're rate-limited by the stream provider "
          f"(reason: {err.reason}) -- a captcha is needed to continue.",
          file=sys.stderr)
    print("Open this in your browser, solve the captcha, then re-run "
          "this command:", file=sys.stderr)
    print(f"  {err.embed}", file=sys.stderr)
    print(file=sys.stderr)


def _fetch(url, referer, as_json=False):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def _decrypt_stream_urls(streams_json):
    """Run the embedded wasm decryptor under node; JSON in, URL list out."""
    d = (streams_json or {}).get("data")
    if not d or not d.get("stream_urls"):
        # Error envelope (seen after rapid back-to-back resolutions: the API
        # answers without data). Don't feed it to the node decryptor.
        code = (streams_json or {}).get("status_code") \
            or (streams_json or {}).get("error") or "no data"
        if str(code) == "404":
            raise RuntimeError(
                "the provider doesn't have this title (status 404) -- it "
                "may not be in their library yet, or not at this id")
        raise RuntimeError(
            f"stream API returned an error envelope ({code}) -- likely "
            "provider rate limiting; wait a bit and re-run (partial "
            "downloads resume automatically)")
    fd, path = tempfile.mkstemp(suffix=".mjs", prefix="leku_vsdec_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(VSDENC_JS)
        proc = subprocess.run(["node", path], input=json.dumps(streams_json),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"vsdec failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def resolve_stream(tid, season=None, episode=None):
    """Follow the vidsrc chain; return (landing_origin, jwt, playlist_urls,
    title, pool_host)."""
    is_tv = bool(season and episode)
    mtype = "tv" if is_tv else "movie"
    q = f"type={mtype}&id={tid}"
    if is_tv:
        q += f"&season={season}&episode={episode}"

    # 1. vs_src.php -> landing URL
    src = _fetch(f"{VIDSRC_ORIGIN}/vs_src.php?{q}", VIDSRC_ORIGIN + "/",
                 as_json=True)["src"]
    land = urllib.parse.urlsplit(src)
    origin = f"{land.scheme}://{land.netloc}"          # e.g. stellarconductornexus.com

    # 2. landing page -> playerUrl
    html = _fetch(src, VIDSRC_ORIGIN + "/")
    m = re.search(r'"playerUrl":"([^"]+)"', html)
    if not m:
        raise RuntimeError("landing page has no playerUrl (layout changed?)")

    # 3. player page -> window.CONFIG
    purl = m.group(1)
    if purl.startswith("/"):
        purl = origin + purl
    phtml = _fetch(purl, origin + "/")
    m = re.search(r"window\.CONFIG\s*=\s*(\{.*?\})\s*;?\s*</script>",
                  phtml, re.S)
    if not m:
        raise RuntimeError("player page has no window.CONFIG")
    cfg = json.loads(m.group(1))
    if not cfg.get("apiToken"):
        # Gated page: no apiToken until a Cloudflare Turnstile captcha is
        # solved (in a real browser). The page states why: "over-cap" means
        # this IP has resolved too many streams recently; solving the captcha
        # in a browser clears it. See HAS_GATE / refreshGateTicket() in the
        # site's player.js.
        embed = f"https://vidsrc.sh/embed/{mtype}/{tid}"
        if is_tv:
            embed += f"/{season}/{episode}"
        raise GatedError(cfg.get("gateReason") or "unknown", embed)
    api_token = cfg["apiToken"]

    # 4. metadata -> pool host
    meta = _fetch(cfg["metaApi"], origin + "/", as_json=True)
    d = meta.get("data") or {}
    pool = d.get("stream_urls_api_host")
    title = d.get("title") or f"tmdb_{tid}"

    # 5. stream_urls (encrypted) on the pool host.
    # Movies: CONFIG.api is the full &stream_urls URL. TV: CONFIG.streamBase
    # needs &season=&episode=&stream_urls appended (see apiFor() in player.js).
    if "api" in cfg:
        base = cfg["api"]
    else:
        base = cfg["streamBase"] + (f"&season={season}&episode={episode}"
                                    if is_tv else "") + "&stream_urls"
    parts = urllib.parse.urlsplit(base)
    pool = pool or parts.netloc
    api_url = urllib.parse.urlunsplit(parts._replace(netloc=pool))
    api_url += ("&" if "?" in api_url else "?") + "api_token=" + api_token
    streams = _fetch(api_url, origin + "/", as_json=True)

    # 6. decrypt via node + wasm
    playlists = _decrypt_stream_urls(streams)
    if not playlists:
        raise RuntimeError("no playlists in decrypted stream_urls")

    # 7. stream-host token (generate.php lives on the STREAM host, not the
    # landing origin — see tokenFor()/originOf() in the site's player.js)
    jwt = _fetch(f"https://{pool}/generate.php?api_token={api_token}",
                origin + "/").strip()
    if not jwt:
        raise RuntimeError("no token from generate.php")

    return origin, jwt, playlists, title, pool


def download(tid, season, episode, out="downloads", fragments=None):
    origin, jwt, playlists, title, pool = resolve_stream(tid, season, episode)
    pool_origin = f"https://{pool}"

    # 8. master -> variant playlist (last variant = highest quality)
    master = _fetch(f"{playlists[0]}?token={jwt}", origin + "/")
    variants = [ln for ln in master.splitlines()
                if ln and not ln.startswith("#")]
    var = variants[-1] if variants else playlists[0]
    if var.startswith("/"):
        var = pool_origin + var
    quality = _fetch(f"{var}?token={jwt}", origin + "/")

    # 9. rewrite segment lines: absolute + token
    lines = []
    for ln in quality.splitlines():
        if ln.startswith("/"):
            lines.append(f"{pool_origin}{ln}?token={jwt}")
        else:
            lines.append(ln)
    # trim for sampling (optional): first `fragments` EXTINF+URI pairs
    if fragments:
        kept, seen = [], 0
        for ln in lines:
            if ln.startswith("#EXTINF"):
                if seen >= fragments:
                    break
                seen += 1
            kept.append(ln)
        lines = kept + ["#EXT-X-ENDLIST"]
    body = "\n".join(lines) + "\n"

    label = f" S{int(season):02d}E{int(episode):02d}" if (season and episode) else ""
    safe = re.sub(r'[<>:"/\\|?*]', "_", title + label).strip(" .") or f"tmdb_{tid}"
    os.makedirs(out, exist_ok=True)
    local = pathlib.Path(tempfile.mkstemp(suffix=".m3u8", prefix=f"leku_{tid}_")[1])
    local.write_text(body, encoding="utf-8")

    print(f"resolved: {title}")
    print(f"playlist: {var}")
    print(f"segments: {sum(1 for l in lines if not l.startswith('#'))}"
          + (f" (sampled to {fragments})" if fragments else ""))
    print(f"-> yt-dlp -> {out}/{safe}.mp4", file=sys.stderr)
    rc = subprocess.call([
        "yt-dlp", "--no-warnings", "--enable-file-urls", "-N", "4",
        "--user-agent", UA,
        "--add-headers", f"Referer: {origin}/",
        # the pool hosts 429 under sustained fragment traffic; a short pause
        # between requests plus generous fragment retries rides it out
        "--sleep-requests", "0.25",
        "--fragment-retries", "15",
        "-o", os.path.join(out, f"{safe}.%(ext)s"),
        local.as_uri(),
    ])
    try:
        local.unlink()
    except OSError:
        pass
    return rc


def download_all(tid, season=None, out="downloads", fragments=None,
                delay=2.0):
    """Download every episode of a show, or of one season. Each episode
    resolves its own fresh provider chain (tokens are single-use); a small
    pause between episodes keeps the provider's per-IP resolution cap happy
    (exceeding it serves captcha-gated pages -- gateReason 'over-cap')."""
    rc = 0
    for s, n in _tv_season_map(tid):
        if season is not None and s != season:
            continue
        for e in range(1, n + 1):
            print(f"\n=== S{s:02d}E{e:02d} ({e}/{n}) ===")
            try:
                if download(tid, str(s), str(e), out=out,
                            fragments=fragments) != 0:
                    rc = 1
            except GatedError as ex:
                if ex.reason == "over-cap":
                    # IP-level: every remaining episode would fail too.
                    _gate_help(ex)
                    return 1
                rc = 1
                print(f"S{s:02d}E{e:02d} gated ({ex.reason}); continuing",
                      file=sys.stderr)
            except Exception as ex:
                rc = 1
                print(f"S{s:02d}E{e:02d} failed ({ex}); continuing",
                      file=sys.stderr)
            if delay and e < n:
                time.sleep(delay)
    return rc


def main():
    p = argparse.ArgumentParser(
        description="drive lekuluent.et: search, stream, download")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--type", default="multi", choices=["multi", "movie", "tv"])
    for name in ("stream", "download"):
        c = sub.add_parser(name)
        c.add_argument("tmdb_id")
        c.add_argument("-s", "--season", default=None)
        c.add_argument("-e", "--episode", default=None)
        if name == "download":
            c.add_argument("--fragments", type=int, default=None,
                           help="download only the first N segments (sample)")
            c.add_argument("--type", default=None, choices=["movie", "tv"],
                           help="namespace for the id (required when it "
                                "exists as both a movie and a show)")
    a = p.parse_args()
    if a.cmd == "search":
        search(a.query, a.type)
    elif a.cmd == "stream":
        stream(a.tmdb_id, a.season, a.episode)
    elif a.cmd == "download":
        try:
            if a.episode:
                # -s and -e given: that tv episode
                sys.exit(download(a.tmdb_id, a.season, a.episode,
                                  fragments=a.fragments))
            if a.season:
                # -s without -e: whole season
                sys.exit(download_all(a.tmdb_id, season=int(a.season),
                                      fragments=a.fragments))
            kind = a.type or _tmdb_kind(a.tmdb_id)
            if kind == "tv":
                # bare show id: every episode of every season
                sys.exit(download_all(a.tmdb_id, fragments=a.fragments))
            # bare movie id: the movie
            sys.exit(download(a.tmdb_id, None, None, fragments=a.fragments))
        except GatedError as ex:
            _gate_help(ex)
            sys.exit(1)
        except RuntimeError as ex:
            # resolution-chain failures (gates, API error envelopes): clean
            # message, no traceback
            print(f"leku: {ex}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
