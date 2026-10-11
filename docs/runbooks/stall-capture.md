# Fetch-stall packet capture (gr465931)

When a fetch to `api.figshare.com`, `api.crossref.org` or `doi.org` hits a
connect / TLS-handshake timeout, the worker starts one bounded `tcpdump`
(`src/precis/utils/stall_capture.py`). Hooks: `retry_transient`
(`utils/http.py`), `_safe_fetch` (`ingest/si_discovery.py`), the Crossref
loop in `workers/bib_parse.py`. Nothing in `safe_fetch.py`.

## Enable (deploy owner; not done by the code change)

1. `tcpdump` installed in the fetcher image/host.
2. Writable capture directory, e.g. a bind mount; set
   `PRECIS_STALL_CAPTURE_DIR=<dir>` in the fetcher worker's environment
   (optional `PRECIS_STALL_CAPTURE_IFACE`, default `any`). Unset = off.
3. Capability, one of:
   - container: `cap_add: [NET_RAW, NET_ADMIN]` and run the worker with
     host networking (or the capture sees only the container's veth, which
     is still the right path for egress);
   - systemd host service: `AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN`;
   - or file capabilities on the binary:
     `setcap cap_net_raw,cap_net_admin+eip $(command -v tcpdump)` (the code
     runs `tcpdump` directly, not via sudo).
   Without capability the capture logs `tcpdump exited ...: Operation not
   permitted` and the fetch carries on unaffected.

## Behaviour

- Rate limit per host: at most 4 per rolling 24 h, at least 1 h apart
  (shared across worker processes via `<dir>/.stall-capture-state.json`).
- Command: `tcpdump -n -i any -s 128 -c 50000 -G 30 -W 1 -w <pcap>
  "tcp port 443 and (host <resolved addrs>)"` - 30 s, under 10 MB.
- Files: `<dir>/stall-<host>-<YYYYmmddTHHMMSSZ>.pcap`; a `.err` file is kept
  only when tcpdump failed. The file name is in the worker log line
  `stall_capture: ... capturing 30s to <path>`.
- Review: `tcpdump -nr <pcap>`; look for SYN/ACK then a ClientHello with no
  ServerHello (retransmits at 1 s, 3 s) to separate path drops from the
  endpoint. Prune old pcaps by hand.
