import {validServer} from './helpers.js';
const $ = id => document.getElementById(id);
const saved = await chrome.storage.local.get(['server','key','status','error']);
if (saved.server) $('server').value=saved.server;
if (saved.key) $('key').value=saved.key;
function display(values) { $('status').textContent=values.status || 'Paste the pairing key from the local app to connect.'; $('status').className=values.error ? 'error' : ''; }
display(saved);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && (changes.status || changes.error)) chrome.storage.local.get(['status','error']).then(display);
});
$('pair').onsubmit=async event => {
  event.preventDefault();
  try {
    const server=validServer($('server').value), key=$('key').value.trim();
    if (!/^[A-Za-z0-9_-]{40,100}$/.test(key)) throw new Error('Copy the complete pairing key from the local app.');
    await chrome.storage.local.set({server,key});
    await chrome.runtime.sendMessage({action:'check'});
  } catch(error) { display({status:error.message,error:true}); }
};
$('check').onclick=() => chrome.runtime.sendMessage({action:'check'}).catch(error => display({status:error.message,error:true}));
