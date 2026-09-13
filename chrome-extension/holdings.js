// Self-contained: Chrome injects this function into the signed-in Yahoo page.
// Reads rendered holdings only. No network requests, cookies, trades, or exports.
export async function readYahooHoldings(expectedURL) {
  let stage='opening Holdings';
  try {
  const started=Date.now(), pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const text=el=>(el?.innerText || el?.textContent || '').replace(/\s+/g,' ').trim();
  const visible=el=>el.getClientRects().length && getComputedStyle(el).visibility!=='hidden';
  const identity=value=>{
    const u=new URL(value), parts=u.pathname.split('/').filter(Boolean);
    return u.origin+'/'+parts[0]+'/'+decodeURIComponent(parts[1] || '');
  };
  const check=()=>{
    if(location.origin!=='https://finance.yahoo.com' || identity(location.href)!==identity(expectedURL))
      throw new Error('Yahoo changed to a different account or sign-in page. Previous holdings were kept.');
    if(Date.now()-started>120000) throw new Error('Holdings collection timed out before reaching the end. Previous holdings were kept.');
  };
  const normalized=s=>s.toLowerCase().replace(/\s+/g,' ').trim();
  const headersOf=table=>Array.from(table.querySelectorAll('thead th,[role="columnheader"]')).filter(visible).map(text);
  const findTable=()=>{
    const matches=Array.from(document.querySelectorAll('table,[role="table"],[role="grid"]')).filter(visible).filter(table=>{
      const headers=headersOf(table).map(normalized);
      return headers.includes('symbol') && headers.some(h=>['shares','quantity','shares held'].includes(h));
    });
    if(matches.length>1) throw new Error('More than one holdings table is visible. Close duplicate views and retry.');
    return matches[0];
  };
  const controls=root=>Array.from(root.querySelectorAll('button,a,[role="button"],[role="tab"]')).filter(visible);
  const label=el=>(el.getAttribute('aria-label') || el.getAttribute('title') || text(el) || el.querySelector?.('svg title')?.textContent || '').replace(/[-_\s]+/g,' ').trim();
  const disabled=el=>el.disabled || el.getAttribute('aria-disabled')==='true' || el.hasAttribute('disabled');
  check();
  let table=findTable(), clicked=false;
  while(!table && Date.now()-started<20000) {
    check();
    if(!clicked) {
      const tabs=controls(document).filter(el=>/^holdings$/i.test(label(el)) && !disabled(el));
      if(tabs.length===1) {tabs[0].click();clicked=true;}
    }
    await pause(250); table=findTable();
  }
  if(!table) {
    const headers=Array.from(document.querySelectorAll('th,[role="columnheader"]')).map(text).slice(0,24);
    throw new Error('No Holdings table with Symbol and Shares columns appeared. Visible headers: '+(headers.join(', ') || 'none')+'.');
  }
  const originalHeaders=headersOf(table), symbolColumn=originalHeaders.map(normalized).indexOf('symbol');
  // Editable portfolios include unlabeled selection/row-action columns. Keep
  // their cells in place so Symbol, Shares and value columns cannot shift.
  const headers=originalHeaders.map((header,index)=>header || `Unlabeled column ${index+1}`);
  const sharesColumn=headers.findIndex(header=>['shares','quantity','shares held'].includes(normalized(header)));
  if(headers.length>80 || new Set(headers.map(normalized)).size!==headers.length)
    throw new Error('Holdings table has ambiguous columns: '+JSON.stringify(originalHeaders)+'. Previous holdings were kept.');
  const readRows=()=>{
    stage='reading table rows';
    check(); table=findTable();
    if(!table || JSON.stringify(headersOf(table))!==JSON.stringify(originalHeaders)) throw new Error('Holdings columns changed while reading. Retry from the Holdings view.');
    const records=[];
    for(const row of table.querySelectorAll('tbody tr,[role="row"]')) {
      const cells=Array.from(row.querySelectorAll('td,[role="cell"],[role="gridcell"]')).filter(visible);
      if(!cells.length || !visible(row)) continue;
      if(cells.length!==headers.length) throw new Error(`A row has ${cells.length} cells for ${headers.length} columns. The table may be loading or contain expanded lots. Previous holdings were kept.`);
      const values=cells.map(text), symbolCell=cells[symbolColumn];
      const quote=symbolCell.querySelector('a[href*="/quote/"]');
      if(quote) values[symbolColumn]=text(quote);
      if(!values[symbolColumn]) throw new Error('A holdings row has no symbol. Previous holdings were kept.');
      // Surface the actual cell when Yahoo mixes a quantity with UI text. Never
      // guess a quantity by taking the first number from an unrecognized cell.
      const shares=values[sharesColumn];
      const numeric=shares.replace(/,/g,'').replace(/−/g,'-');
      if(!/^(?:|[-–—]|--|n\/a|na|null|add)$/i.test(shares)
         && !/^(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|\((?:\d+(?:\.\d*)?|\.\d+)\))$/i.test(numeric))
        throw new Error(`Shares for ${values[symbolColumn]} contains unrecognized text ${JSON.stringify(shares).slice(0,180)}. Previous holdings were kept.`);
      records.push(values);
    }
    return records;
  };
  const nextLabel=value=>/^(?:go\s*to\s+)?next(?: holdings)?(?:\s*page)?$/i.test(value);
  const rangePattern=/(?:showing\s+)?(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)\s+(?:of|out of)\s+(\d[\d,]*)/ig;
  const scope=()=>{
    // Pagination can be outside the table's immediate wrapper.
    for(let root=table.parentElement;root;root=root.parentElement) {
      if([...text(root).matchAll(rangePattern)].length || controls(root).some(el=>nextLabel(label(el)))) return root;
    }
    return document.body;
  };
  const paging=()=>{
    stage='checking pagination';
    const root=scope();
    const matches=[...text(root).matchAll(rangePattern)];
    if(matches.length>1) throw new Error('More than one pagination range was found. Cannot confirm holdings completeness.');
    const range=matches[0]?.slice(1).map(x=>Number(x.replace(/,/g,'')));
    const belongsToPagination=el=>{
      const controlled=el.getAttribute('aria-controls');
      if(table.id && controlled?.split(/\s+/).includes(table.id)) return true;
      const navigation=el.closest?.('[aria-label*="pagination" i],[data-testid*="pagination" i],[data-test*="pagination" i],[class*="pagination" i],[class*="pager" i]');
      // A pagination container must sit alongside this table, not another widget.
      if(navigation && root.contains?.(navigation) && root!==document.body && root!==document.documentElement) return true;
      return false;
    };
    let next=controls(root).filter(el=>nextLabel(label(el)) && (range || belongsToPagination(el)));
    // The last page is complete even when Yahoo leaves its Next control enabled.
    if(range && range[1]>=range[2]) next=[];
    if(next.length>1) throw new Error('More than one Next control was found. Cannot safely page through holdings.');
    const more=controls(root).filter(el=>/^(?:show|load) more(?: holdings| positions| rows)?$/i.test(label(el))
      && (/(?:holdings|positions|rows)$/i.test(label(el)) || belongsToPagination(el)
          || table.parentElement?.contains?.(el)));
    return {range,next:next[0],more:more.find(el=>!disabled(el))};
  };
  const rows=[], visited=new Set();
  let pages=0, expectedCount=null, cashRow=null;
  const isCash=row=>/^total cash$/i.test(row[symbolColumn]);
  while(pages<200) {
    let stableSince=Date.now(), previousSymbols='', observedCount=0, currentRows=[], priorSymbols=[];
    while(true) {
      check(); currentRows=readRows();
      if(currentRows.length<observedCount) throw new Error('Yahoo virtualized or removed holdings rows while scrolling. Cannot verify a complete capture; previous holdings were kept.');
      const nextSymbols=currentRows.map(r=>r[symbolColumn]);
      const remaining=[...nextSymbols];
      for(const old of priorSymbols) {
        const index=remaining.indexOf(old);
        if(index<0) throw new Error('Yahoo replaced holdings rows while scrolling. A virtualized or changing table cannot be confirmed complete; previous holdings were kept.');
        remaining.splice(index,1);
      }
      priorSymbols=nextSymbols; observedCount=currentRows.length;
      const symbols=JSON.stringify(nextSymbols);
      if(symbols!==previousSymbols) {previousSymbols=symbols;stableSince=Date.now();}
      const {more}=paging();
      if(more) {more.click();stableSince=Date.now();await pause(500);continue;}
      // Scroll the table container and page to trigger rows loaded on demand.
      for(let el=table.parentElement;el && el!==document.body;el=el.parentElement) {
        if(el.scrollHeight>el.clientHeight+2 && /auto|scroll/.test(getComputedStyle(el).overflowY))
          el.scrollTop=el.scrollHeight;
      }
      window.scrollTo(0,document.documentElement.scrollHeight);
      await pause(250);
      if(currentRows.length && Date.now()-stableSince>=1500) {
        const latest=readRows();
        if(JSON.stringify(latest.map(r=>r[symbolColumn]))!==symbols) continue;
        currentRows=latest; break;
      }
      if(!currentRows.length && Date.now()-stableSince>=15000) throw new Error('The Holdings table has no readable rows. Existing holdings were kept; an empty account has not been assumed.');
    }
    const signature=JSON.stringify(currentRows.map(r=>r[symbolColumn]));
    const positions=currentRows.filter(r=>!isCash(r));
    const cashRows=currentRows.filter(isCash);
    if(cashRows.length>1) throw new Error('Multiple Total Cash rows were found. Cannot verify the portfolio total.');
    if(cashRows.length) cashRow=cashRows[0];
    if(visited.has(signature)) throw new Error('Yahoo did not advance to a new holdings page. Previous holdings were kept.');
    visited.add(signature); pages++;
    const {range,next}=paging();
    if(range) {
      const [first,last,total]=range;
      if(first!==rows.length+1 || last-first+1!==positions.length || (expectedCount!==null && total!==expectedCount))
        throw new Error('The holdings row count does not match Yahoo’s pagination. Previous holdings were kept.');
      expectedCount=total;
    }
    rows.push(...positions);
    if(rows.length>50000) throw new Error('Holdings exceed the 50,000 row limit.');
    if(!next || disabled(next)) {
      if(expectedCount!==null && rows.length!==expectedCount) throw new Error(`Read ${rows.length} of ${expectedCount} holdings; remaining rows were not accessible. Previous holdings were kept.`);
      // Detect ARIA row totals too (header row is included in aria-rowcount).
      const ariaCount=Number(table.getAttribute('aria-rowcount'));
      if(expectedCount===null && ariaCount>0) {
        expectedCount=ariaCount-1-(cashRow?1:0);
        if(rows.length!==expectedCount) throw new Error(`Read ${rows.length} of ${expectedCount} table rows. Previous holdings were kept.`);
      }
      if(cashRow) rows.push(cashRow);
      if(expectedCount!==null && cashRow) expectedCount++;
      // Yahoo's Add prompt means no quantity is recorded, not zero shares.
      // Retain every row for pagination reconciliation and keep its exact Shares
      // display in the raw snapshot. The importer excludes null quantities from
      // the positions view while retaining their other captured information.
      let outputHeaders=headers, outputRows=rows;
      if(rows.some(row=>/^add$/i.test(row[sharesColumn]))) {
        const displayHeader='Yahoo Shares display';
        if(headers.some(h=>normalized(h)===normalized(displayHeader)))
          throw new Error('Holdings table conflicts with the Shares display column. Previous holdings were kept.');
        outputHeaders=[...headers,displayHeader];
        outputRows=rows.map(row=>{
          const display=row[sharesColumn], values=[...row,display];
          if(/^add$/i.test(display)) values[sharesColumn]='';
          return values;
        });
      }
      return {ok:true,url:location.href, table:{method:'yahoo-holdings-table-v1',headers:outputHeaders,rows:outputRows,page_count:pages,
        expected_count:expectedCount, completeness:expectedCount===null?'end-observed':'count-verified',
        captured_at:new Date().toISOString()}};
    }
    next.click();
    const waitStarted=Date.now();
    while(JSON.stringify(readRows().map(r=>r[symbolColumn]))===signature) {
      check();if(Date.now()-waitStarted>15000) throw new Error('Yahoo’s Next control did not load another holdings page. Previous holdings were kept.');
      await pause(250);
    }
  }
  throw new Error('Too many holdings pages. Previous holdings were kept.');
  } catch(error) {
    let url=null;
    try {url=location.href;} catch {}
    return {ok:false,url,error:`${stage}: ${error?.message || String(error)}`.slice(0,650)};
  }
}
