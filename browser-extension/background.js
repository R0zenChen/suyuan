chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'openSciHubResolver') { return; }
  const doi = /^10\.\d{4,9}\/[\w.()\-;/:]+$/i.exec(String(message.doi || ''))?.[0];
  if (!doi) { sendResponse({ ok: false }); return; }
  chrome.tabs.create({ url: chrome.runtime.getURL(`resolve.html?doi=${encodeURIComponent(doi)}`) })
    .then(() => sendResponse({ ok: true }))
    .catch(() => sendResponse({ ok: false }));
  return true;
});
