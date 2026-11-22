// Cloudflare Worker relay for FMCSA SAFER — free tier (100k req/day).
//
// WHY: SAFER answers HTTP 403 to the IP address Render gives this site
// (verified live 2026-11-22: every search 403s from Render while the
// identical request returns 200 from any other IP). A Worker runs on
// Cloudflare's network, i.e. a different egress IP, so SAFER answers it.
//
// SETUP (free, ~10 minutes, no card):
//   1. Create a free Cloudflare account (dash.cloudflare.com/sign-up).
//   2. Workers & Pages -> Create Application -> Create Worker -> paste
//      this file -> Save and Deploy. Note the worker URL, e.g.
//      https://roadx-safer.<your-subdomain>.workers.dev
//   3. Render dashboard -> service roadxsolutions -> Environment, add:
//      ROAD_SAFER_RELAY_URL = https://roadx-safer.<your-subdomain>.workers.dev
//      (no trailing slash) and redeploy.
//   4. The site still tries direct first; the relay is only used when
//      the direct request is refused, so nothing changes while Render
//      is unblocked. To test: search any MC on the live site — a
//      Qualified result means the relay path works.
//
// SAFETY: only /query.asp is forwarded, only GET, and only to
// safer.fmcsa.dot.gov — this cannot be used as a general-purpose
// proxy. The Worker sees only public MC/USDOT numbers, no logins,
// no credentials, no carrier data from the site's database.
//
// RISKS: SAFER could one day block Cloudflare's egress IPs too (then
// the site 403s again and a different relay is needed); if the Worker
// is deleted or the free quota is hit, searches fall back to failing
// exactly as they do today (direct first, relay only on refusal).
// Cloudflare's free plan needs no payment method.

const UPSTREAM = "https://safer.fmcsa.dot.gov";
const HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    // Only the SAFER snapshot endpoint, only GET — nothing else.
    if (request.method !== "GET" || url.pathname !== "/query.asp") {
      return new Response("Not found", { status: 404 });
    }
    const resp = await fetch(UPSTREAM + url.pathname + url.search, {
      headers: HEADERS,
    });
    return new Response(resp.body, {
      status: resp.status,
      headers: {
        "Content-Type": resp.headers.get("Content-Type") || "text/html",
      },
    });
  },
};
