// Meltwater search API client.
//
// Three-stage call chain, exactly as the Meltwater SPA does it:
//   1. GetToken (GraphQL)  -> a search token
//   2. af.meltwater.io/data/runes -> the token resolves to a low-level rune query
//   3. unified-search.../msearch  -> the actual search, paginated
//
// The SPA's own headers are mandatory. Omitting the Apollo client headers makes
// GetToken answer HTTP 500 "Internal server error" (not 400/401), which reads
// like a server fault but is really a missing-header rejection.
import { CDP, readDevToolsEndpoint, sleep } from './cdp.mjs';

export const GRAPHQL_URL = process.env.MW_GRAPHQL_URL || 'https://discovery-next-mw-apollo-production.meltwater.io/graphql';
export const RUNES_URL = process.env.MW_RUNES_URL || 'https://af.meltwater.io/data/runes';
export const MSEARCH_BASE = process.env.MW_MSEARCH_BASE || 'https://unified-search.meltwater.io/1.0/accounts';
export const ACCOUNT_ID = process.env.MW_ACCOUNT_ID || '';

const GET_TOKEN_QUERY = `query GetToken($searchCriteria: SearchQueryInput!, $elsEntities: [KgEntityInput!], $dateStart: String, $dateEnd: String) {
  token(searchQuery: $searchCriteria, elsEntities: $elsEntities, dateStart: $dateStart, dateEnd: $dateEnd) {
    token
    __typename
  }
}`;

export const GRAPHQL_HEADERS = {
  accept: 'application/graphql-response+json,application/json;q=0.9',
  'apollographql-client-name': 'discovery-next',
  'apollographql-client-version': '0.0.0',
  'content-type': 'application/json'
};

export const RUNES_HEADERS = {
  'content-type': 'application/json',
  'x-client-name': 'alpha-curator-web-overview'
};

export const MSEARCH_HEADERS = {
  'content-type': 'application/json',
  'x-product-type': 'explore-dataservice',
  'x-credit-pool-id': 'mi-explore-brand-volume-ip'
};

export const MSEARCH_URL = msearchUrl(ACCOUNT_ID);

/** The index refuses a window wider than 370 days. */
export const MAX_WINDOW_DAYS = 370;

/**
 * The search backend reports failures INSIDE a 200 response.
 * A missing date clause, an over-wide window or a malformed rune all come back
 * as HTTP 200 with an `error` object in the body, so checking res.ok alone
 * turns a failure into a silently empty result set.
 */
export function assertNoBodyError(parsed, where) {
  const err = parsed?.error || parsed?.errors ||
    parsed?.response?.error || parsed?.response?.errors;
  if (err) {
    const msg = err.message || err.errorType || JSON.stringify(err).slice(0, 300);
    const type = err.errorType || err.code || 'unknown';
    throw new Error(`${where} returned a body error (HTTP 200): ${type} - ${msg}`);
  }
  return parsed;
}

export function msearchUrl(accountId = ACCOUNT_ID) {
  return `${MSEARCH_BASE}/${accountId}/msearch`;
}

/** Build the SearchQueryInput payload. booleanQuery is passed through verbatim. */
export function buildSearchCriteria({ booleanQuery, informationTypes = ['news'], savedSearchId = '' }) {
  return {
    allKeywords: [], anyKeywords: [], notKeywords: [],
    allSearches: [], anySearches: [], notSearches: [],
    caseSensitive: 'no',
    searchQueryType: 'boolean',
    informationTypes,
    typedFilter: { universalFilters: {} },
    savedSearchId,
    booleanQuery
  };
}

/**
 * Turn a plain local date into a UTC ISO instant.
 * @param {string} date  'YYYY-MM-DD'
 * @param {boolean} endOfDay  true -> 23:59:59.999 local
 * @param {number} tzOffsetMinutes  local offset from UTC, e.g. 480 for Asia/Shanghai
 */
export function dayBoundary(date, endOfDay = false, tzOffsetMinutes = 480) {
  const [y, m, d] = date.split('-').map(Number);
  const hh = endOfDay ? 23 : 0, mm = endOfDay ? 59 : 0, ss = endOfDay ? 59 : 0, ms = endOfDay ? 999 : 0;
  const localUtcMs = Date.UTC(y, m - 1, d, hh, mm, ss, ms);
  return new Date(localUtcMs - tzOffsetMinutes * 60000).toISOString();
}

/** Attach to the Meltwater page and grab a live bearer token from the app's own traffic. */
export async function connectAndAuth({ urlMatch = /app\.meltwater\.com/ } = {}) {
  const endpoint = readDevToolsEndpoint();
  if (!endpoint) {
    throw new Error(
      'No Chrome DevTools endpoint found. Open Chrome, visit chrome://inspect/#remote-debugging, ' +
      'enable "Allow remote debugging for this browser instance", then reopen Meltwater.'
    );
  }
  const cdp = await CDP.connect(endpoint);
  try {
    const targets = await cdp.listTargets();
    const page = targets.find((t) => t.type === 'page' && urlMatch.test(t.url));
    if (!page) throw new Error('No Meltwater page tab found. Open app.meltwater.com in the logged-in Chrome.');
    const sessionId = await cdp.attachToTarget(page.targetId);
    await cdp.send('Page.enable', {}, sessionId);
    await cdp.send('Runtime.enable', {}, sessionId);
    await cdp.send('Network.enable', {}, sessionId);
    const auth = await captureAuth(cdp, sessionId);
    return { cdp, sessionId, token: auth.token, accountId: auth.accountId, targetUrl: page.url };
  } catch (error) {
    cdp.close();
    throw error;
  }
}

/**
 * Decode a JWT payload and return its expiry in SECONDS.
 * Meltwater's JWT is non-standard: `exp` is in MILLISECONDS. Comparing a
 * millisecond value against Date.now()/1000 is always true, so a token cached
 * that way never expires and the script keeps using a dead token.
 */
export function tokenExpirySeconds(token) {
  try {
    const p = JSON.parse(Buffer.from(token.split('.')[1], 'base64').toString());
    if (typeof p.exp !== 'number') return null;
    return p.exp > 1e11 ? Math.floor(p.exp / 1000) : p.exp;
  } catch { return null; }
}

/**
 * The SPA is idle most of the time and fires no authenticated request, so the
 * token has to be provoked: focus the Monaco editor and send Ctrl+Enter to
 * re-run the current search, then read the Authorization header off the wire.
 * Sometimes that still does not produce an authenticated call, so fall back to
 * reloading the page -- the SPA re-runs its startup queries on boot.
 */
export async function captureAuth(cdp, sessionId, { timeoutMs = 45000 } = {}) {
  let found = null;
  let accountId = null;
  const off = cdp.on('Network.requestWillBeSent', (p) => {
    const h = p.request.headers || {};
    const auth = h.authorization || h.Authorization;
    if (auth && auth.startsWith('Bearer ') && /meltwater\.io/.test(p.request.url)) found = auth;
    const match = (p.request.url || '').match(/\/accounts\/([a-f0-9]+)\//i);
    if (match) accountId = match[1];
  }, sessionId);

  const waitFor = async (ms) => {
    const t0 = Date.now();
    while (!found && Date.now() - t0 < ms) await sleep(250);
    return found;
  };

  try {
    await cdp.evaluate(sessionId,
      `(() => { const ta = document.querySelector('textarea.ime-text-area'); if (ta) ta.focus(); return 1; })()`);
    for (const type of ['keyDown', 'keyUp']) {
      await cdp.send('Input.dispatchKeyEvent', {
        type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: 2
      }, sessionId);
    }
  } catch (e) {
    console.error('warn: could not provoke a Meltwater request:', e.message);
  }

  // Ctrl+Enter may only produce unauthenticated telemetry calls; reload as a fallback.
  if (!(await waitFor(8000))) {
    try {
      await cdp.send('Page.reload', { ignoreCache: false }, sessionId);
    } catch (e) {
      console.error('warn: reload fallback failed:', e.message);
    }
  }

  await waitFor(timeoutMs);
  off();
  if (!found) throw new Error('Timed out waiting for an authenticated Meltwater request.');
  return { token: found, accountId };
}

export async function getSearchToken(auth, searchCriteria, dateStart, dateEnd) {
  const r = await fetch(GRAPHQL_URL, {
    method: 'POST',
    headers: { ...GRAPHQL_HEADERS, authorization: auth },
    body: JSON.stringify({
      operationName: 'GetToken',
      variables: { searchCriteria, elsEntities: [], dateStart, dateEnd },
      extensions: { clientLibrary: { name: '@apollo/client', version: '4.2.8' } },
      query: GET_TOKEN_QUERY
    }),
    signal: AbortSignal.timeout(120000)
  });
  if (!r.ok) throw new Error(`GetToken HTTP ${r.status}: ${(await r.text()).slice(0, 400)}`);
  const j = await r.json();
  if (j.errors) throw new Error('GetToken errors: ' + JSON.stringify(j.errors).slice(0, 600));
  const t = j?.data?.token?.token;
  if (!t) throw new Error('GetToken returned no token: ' + JSON.stringify(j).slice(0, 400));
  return t;
}

export async function getRunes(auth, token) {
  const r = await fetch(RUNES_URL, {
    method: 'POST',
    headers: { ...RUNES_HEADERS, authorization: auth },
    body: JSON.stringify({ token }),
    signal: AbortSignal.timeout(120000)
  });
  if (!r.ok) throw new Error(`runes HTTP ${r.status}: ${(await r.text()).slice(0, 400)}`);
  return await r.json();
}

/**
 * Paginated search.
 *
 * `pagination.group` is intentionally omitted. With group={method:'similar'}
 * the API collapses near-duplicate articles into a representative plus a
 * `grouped[]` array that does not expose every member, and `total` counts
 * documents while `start` indexes groups -- so a paginated harvest silently
 * recovers only part of the result set. Without grouping each hit is one raw
 * document and start/limit paging returns every document exactly once.
 */
export async function msearch(auth, runeQuery, dateFrom, dateTo, {
  limit = 100, start = 0, sortOrder = 'desc', accountId = ACCOUNT_ID
} = {}) {
  const body = {
    requests: [{
      id: 'query_21',
      dataSource: 'general-index',
      request: {
        query: {
          and: [
            { lowLevel: { rune: runeQuery } },
            { hiddenDocuments: 'excluded' },
            { date: { from: dateFrom, to: dateTo } }
          ]
        },
        timezone: 'Asia/Shanghai',
        pagination: { sort: { on: 'date', order: sortOrder }, limit, start },
        documentFormat: { highlight: true }
      },
      outliers: null, interval: null, language: null, analysis: null, metrics: null
    }]
  };
  const r = await fetch(msearchUrl(accountId), {
    method: 'POST',
    headers: { ...MSEARCH_HEADERS, authorization: auth },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(180000)
  });
  if (!r.ok) throw new Error(`msearch HTTP ${r.status}: ${(await r.text()).slice(0, 400)}`);
  const parsed = await r.json();
  return assertNoBodyError(parsed, 'msearch');
}

/**
 * Resolve a boolean query + date window into a runnable rune query.
 * Pass the WHOLE runesQuery object to msearch -- runesQuery.allQueries[0] is
 * only the user query and drops the content-type filter the server attached.
 */
export async function resolveRuneQuery(auth, { booleanQuery, informationTypes = ['news'], dateStart, dateEnd }) {
  const sc = buildSearchCriteria({ booleanQuery, informationTypes });
  const token = await getSearchToken(auth, sc, dateStart, dateEnd);
  const runes = await getRunes(auth, token);
  const runeQuery = runes.runesQuery ?? runes;
  if (!runeQuery) throw new Error('Could not resolve runeQuery: ' + JSON.stringify(runes).slice(0, 400));
  return runeQuery;
}

/** One search, single page. */
export async function runSearch(auth, { booleanQuery, dateStart, dateEnd, informationTypes = ['news'], limit = 100, start = 0 }) {
  const runeQuery = await resolveRuneQuery(auth, { booleanQuery, informationTypes, dateStart, dateEnd });
  const res = await msearch(auth, runeQuery, dateStart, dateEnd, { limit, start });
  // The response is a single object, not an array of requests.
  const resp = res?.response ?? res?.requests?.[0]?.response;
  if (!resp) throw new Error('msearch returned no response: ' + JSON.stringify(res).slice(0, 400));
  return { total: resp.total ?? 0, hits: resp.hits ?? [], took: resp.took, runeQuery };
}

/**
 * Map the platform's content-type groups to the facet values the index stores.
 * The UI's "news" group is news + tender (tender = procurement notices).
 */
export const INFO_TYPE_FACETS = {
  news: ['news', 'tender'],
  social: ['social'],
  broadcast: ['broadcast'],
  tender: ['tender'],
  all: ['social', 'news', 'tender', 'broadcast']
};

export function facetsFor(informationTypes = ['news']) {
  if (!informationTypes || !informationTypes.length) return INFO_TYPE_FACETS.all;
  const out = new Set();
  for (const t of informationTypes) for (const v of (INFO_TYPE_FACETS[t] || [t])) out.add(v);
  return [...out];
}

/**
 * Single-request path: put the boolean query straight into msearch instead of
 * going through GetToken -> runes. One request per page instead of three, and
 * the results are identical.
 *
 * The content-type filter MUST be a structured `lowLevel.rune` facet. Putting
 * `metaData.source.informationType:"news"` inside the boolean string is
 * silently ignored (it becomes a plain search term) and the query then spans
 * the entire corpus.
 */
export async function msearchBoolean(auth, {
  booleanQuery, dateStart, dateEnd, informationTypes = ['news'], limit = 25, start = 0
}) {
  const facets = facetsFor(informationTypes);
  // The facet terms are OR-ed, not AND-ed: a document is never both `news` and
  // `tender`, so wrapping them in type:"all" yields an empty intersection (0 hits
  // for every query). The Meltwater SPA uses type:"any" for exactly this filter.
  const facetRune = {
    type: 'any',
    anyQueries: facets.map((v) => ({ type: 'term', field: 'metaData.source.informationType', value: v }))
  };
  const body = {
    requests: [{
      id: 'query_21',
      dataSource: 'general-index',
      request: {
        query: {
          and: [
            { date: { from: dateStart, to: dateEnd } },
            { meltwaterBoolean: { query: booleanQuery, caseSensitive: 'no' } },
            { lowLevel: { rune: { type: 'all', allQueries: [facetRune] } } }
          ]
        },
        pagination: { limit, start, sort: { on: 'date', order: 'desc' } },
        fetchTotal: true,
        documentFormat: { consumerAttributes: true, explorePlusAttributes: true }
      }
    }]
  };
  const r = await fetch(MSEARCH_URL, {
    method: 'POST',
    headers: { ...MSEARCH_HEADERS, authorization: auth },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(180000)
  });
  if (!r.ok) throw new Error(`msearch HTTP ${r.status}: ${(await r.text()).slice(0, 400)}`);
  const parsed = assertNoBodyError(await r.json(), 'msearch');
  const resp = parsed?.response ?? parsed?.requests?.[0]?.response;
  if (!resp) throw new Error('msearch returned no response: ' + JSON.stringify(parsed).slice(0, 400));
  return { total: resp.total ?? 0, hits: resp.hits ?? [], took: resp.took };
}

/** Reject a window the index would refuse, before burning a request on it. */
export function assertWindow(dateStart, dateEnd, maxDays = MAX_WINDOW_DAYS) {
  const days = (new Date(dateEnd) - new Date(dateStart)) / 86400000;
  if (days > maxDays) {
    throw new Error(`Date range (${Math.round(days)} days) exceeds the ${maxDays}-day maximum. Split it into segments.`);
  }
  return days;
}
