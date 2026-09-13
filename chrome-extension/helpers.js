export function portfolioURL(value) {
  try {
    const url = new URL(value);
    const parts = url.pathname.split('/').filter(Boolean);
    const identifier = decodeURIComponent(parts[1] || '');
    if (url.origin !== 'https://finance.yahoo.com' || url.username || url.password || !['portfolio', 'portfolios'].includes(parts[0]) ||
        !/^(?:[A-Za-z0-9_-]+|yodlee\|[A-Za-z0-9_-]+)$/.test(identifier) || ['new','create','import','compare'].includes(identifier.toLowerCase())) return null;
    return url.origin + '/' + parts[0] + '/' + encodeURIComponent(identifier);
  } catch { return null; }
}

export function validServer(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || url.username || url.password ||
      !['', '/'].includes(url.pathname) || url.search || url.hash) throw new Error('Use the local app address, such as http://127.0.0.1:8765.');
  return url.origin;
}

export function validateJob(job) {
  if (!job || !/^[a-f0-9]{32}$/.test(job.id) || !['connect','discover','refresh'].includes(job.action))
    throw new Error('The local app returned an invalid request.');
  if (job.action === 'refresh' ? portfolioURL(job.url) !== job.url : job.url !== 'https://finance.yahoo.com/portfolios/')
    throw new Error('The local app requested an unsupported Yahoo page.');
  if (job.navigation_url && (job.action === 'refresh' ? portfolioURL(job.navigation_url) !== job.url : job.navigation_url !== job.url))
    throw new Error('The portfolio navigation address does not match the selected account.');
  return job;
}

export async function waitForYahooTab(getTab, target, {timeout=45000, settle=600, pause=ms=>new Promise(r=>setTimeout(r,ms))}={}) {
  const expected=portfolioURL(target), end=Date.now()+timeout;
  let stable=0, last='';
  while(Date.now()<end) {
    const tab=await getTab();
    const matches=expected ? portfolioURL(tab.url)===expected : (!tab.url || tab.url.replace(/\/$/,'')===target.replace(/\/$/,''));
    if(matches && tab.status==='complete' && !tab.pendingUrl) {
      if(tab.url!==last || !stable) {stable=Date.now();last=tab.url;}
      if(Date.now()-stable>=settle) return tab;
    } else stable=0;
    await pause(200);
  }
  throw new Error('Yahoo did not finish opening the selected portfolio. Complete sign-in in Chrome and retry; previous holdings were kept.');
}

// This function is injected only into Yahoo Finance, never Google or a login page.
export async function yahooPageAction(action) {
  if (location.origin !== 'https://finance.yahoo.com') throw new Error('Finish Yahoo sign-in in Chrome first.');
  if (action === 'discover') {
    const read = () => {
      const links = [...document.querySelectorAll('a[href]')]
        .filter(a => /\/portfolios?\/[^/?#]+/.test(a.getAttribute('href')) && a.innerText.trim())
        .map(a => ({href:a.href, text:a.innerText.trim()}));
      const names = [];
      for (const table of document.querySelectorAll('table,[role="table"]')) {
        const headers = [...table.querySelectorAll('th,[role="columnheader"]')];
        const column = headers.findIndex(h => /^portfolio name$/i.test(h.innerText.trim()));
        if (column < 0) continue;
        for (const row of table.querySelectorAll('tr,[role="row"]')) {
          const cell = row.querySelectorAll('td,[role="cell"]')[column];
          if (cell?.innerText.trim()) names.push(cell.innerText.trim());
        }
      }
      return {links:links.slice(0, 2000), portfolio_names:[...new Set(names)].slice(0, 2000)};
    };
    const started = Date.now();
    let signature = '', stableSince = started, result;
    // Yahoo can load linked brokerage rows after manually created portfolios.
    while (Date.now() - started < 20000) {
      result = read();
      const nextSignature = JSON.stringify(result);
      if (nextSignature !== signature) { signature = nextSignature; stableSince = Date.now(); }
      const namedLinks = new Set(result.links.map(link => link.text));
      const covered = result.portfolio_names.every(name => namedLinks.has(name));
      if (result.links.length && covered && Date.now() - started >= 2000 && Date.now() - stableSince >= 1200) return result;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    if (!result?.links.length) throw new Error('No portfolio links appeared. Finish Yahoo sign-in in Chrome, then retry.');
    return result;
  }
  throw new Error('Unsupported Yahoo page action.');
}
