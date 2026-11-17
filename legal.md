# Risk profile — running leku in the USA

What legal/enforcement exposure you realistically take on by using `leku.py`
for personal use in the United States. (Informational, not legal advice.)

## What leku actually does, technically

- Sends **plain HTTPS requests** to the same public endpoints the lekuluent.et
  website's own player uses, from your own IP, one connection at a time.
- Downloads **unencrypted HLS** playlists and segments. There is no DRM
  (Widevine/FairPlay) involved, so nothing is *decrypted* — leku reproduces
  the token/signing handshake the provider's own JavaScript performs in every
  visitor's browser.
- **No peer-to-peer**: no swarm, no uploading, no distribution to others.

## The comparison people usually make: torrenting

BitTorrent enforcement in the US is almost entirely **swarm monitoring** —
rightsholders' agents join a torrent, log the IPs of peers *uploading* to
them, and trigger the DMCA notices your ISP forwards. The mechanism depends
on you being publicly **distributing** in the swarm.

leku has no swarm presence. Copyright trolls cannot see your downloads;
there is no equivalent monitoring infrastructure for "watched/ripped a stream
over HTTPS." Your ISP can see traffic volume and destination hostnames (TLS
exposes SNI, not content), but volume-based strikes from direct HTTPS
downloads are essentially unheard of.

| Vector | Torrenting | leku |
|---|---|---|
| Visible to rightsholder monitors | yes (in swarm) | no |
| You distribute/upload | yes | no |
| DMCA-forwarding mechanism exists | mature, automated | none standard |
| ISP sees what you're doing | peers + tracker | hostname + volume only |

## Theoretical exposure vs. practical exposure

**Theoretical.** Downloading a copyrighted movie is a reproduction; personal use
is not a guaranteed defense (US law has no general "private copying" safe
harbor for this). Willful statutory damages run up to $150,000 per work.

**Practical.** Suits against individual *downloaders/viewers* of streams are
nearly nonexistent. US enforcement targets:

1. **Site/provider operators** (seizures, ACE/criminal actions) — that risk
   sits on lekuluent.et and the embed providers, not you.
2. **Distributors/uploaders** (torrent seeders, release groups).
3. End users, only where cheap automated detection exists — i.e., **P2P
   swarms**, which leku avoids.

The realistic worst case for a leku user is that a provider or site goes
offline. A rightsholder somehow obtaining and pursuing downloader logs is
possible in principle but has no meaningful precedent at this delivery method,
and would be far harder and costlier than swarm monitoring.

## Anti-circumvention (DMCA §1201)

leku does not break DRM — the streams carry none. It replays the provider's
own token handshake (signed URLs, ChaCha20 blob decrypted by the provider's
own wasm), which is the same thing every browser visiting the site does.
§1201 claims are aimed at DRM circumvention; nothing here circumvents a
"technological measure" in the classic sense. This materially distinguishes
it from tools that strip Widevine.

## Red flags that *would* raise your profile

- **Re-uploading or sharing** what you download (now you're a distributor —
  the actually-prosecuted category).
- **Running leku as a paid service / at scale** (commercial flavor makes it
  attractive if anyone ever came looking).
- Seeding the same content via BitTorrent alongside leku — that's where
  notices come from, not from leku.

## Bottom line

For personal use, leku's traffic is indistinguishable from someone watching
the site in a browser, with no P2P swarm for anyone to monitor. Copyright
notices and the occasional end-user lawsuit in the US are overwhelmingly a
**BitTorrent-distribution** phenomenon. Run it for yourself, keep what you
watch private, and your practical risk is about as low as this activity gets —
while noting that "unlikely to be enforced" is not the same as "authorized."
