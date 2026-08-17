# YouTube / Streamlink notes

The setup deliberately treats YouTube more conservatively than ordinary HLS sources.

- Starts from **Start all** and server autostart are staggered by 5 seconds.
- Normal supervisor retries start at 30 seconds and grow to 180 seconds.
- YouTube gets only one internal `--retry-streams=30` retry.
- `LOGIN_REQUIRED` / bot verification gets a 10-minute cooldown.
- `youtube-cookies.txt` is optional, local-only and ignored/protected from Git.
- The built-in Streamlink YouTube plugin already sets a Chrome User-Agent, so the setup does not add its own YouTube User-Agent header.

## If YouTube still challenges the installation

Streamlink's maintainer has described `LOGIN_REQUIRED: Sign in to confirm you're not a bot` as an IP-address challenge. Cookies can help in some cases, but Streamlink's YouTube plugin does not implement normal account authentication and there are current reports where cookies still do not clear the challenge.

The other Streamlink-supported network-side option is a stable HTTP or SOCKS proxy (`--http-proxy`). A clean, stable egress IP is preferable to aggressively rotating addresses. For SOCKS, `socks5h://...` also moves DNS resolution to the proxy.

For this installation, a sensible escalation path is:

1. stagger starts + conservative retries (already enabled),
2. current Streamlink release,
3. optional browser cookie export,
4. if the public installation IP remains challenged, route **YouTube only** through a stable proxy/tunnel with a clean egress IP,
5. if Streamlink still rejects the same stream while another extractor succeeds, consider a YouTube-specific fallback rather than making Streamlink retry more aggressively.
