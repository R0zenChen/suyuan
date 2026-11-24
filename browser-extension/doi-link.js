(() => {
  const doiPattern = /\b10\.\d{4,9}\/[\w.()\-;/:]+/i;
  let scheduled = false;

  function addLink() {
    const targets = [...document.querySelectorAll('a[href*="doi.org/10."], [class*="doi" i], [id*="doi" i]')].slice(0, 60);
    const target = targets.find((element) => doiPattern.test(`${element.textContent || ''} ${element.getAttribute('href') || ''}`));
    if (!target || !target.parentElement || target.parentElement.querySelector('.suyuan-scihub-link')) { return; }
    const doi = doiPattern.exec(`${target.textContent || ''} ${target.getAttribute('href') || ''}`)?.[0]?.replace(/[.,;)]$/, '');
    if (!doi) { return; }
    const link = document.createElement('a');
    link.className = 'suyuan-scihub-link';
    link.href = `https://doi.org/${encodeURIComponent(doi)}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'Sci-Hub ↗';
    link.title = '按 DOI 检查可用且收录该论文的 Sci-Hub 镜像';
    link.style.cssText = 'display:inline-block;margin-inline-start:8px;padding:2px 7px;border-radius:5px;background:#e8f3ed;color:#17533f;font:600 12px/1.5 sans-serif;text-decoration:none;vertical-align:middle';
    link.addEventListener('click', (event) => {
      event.preventDefault();
      chrome.runtime.sendMessage({ type: 'openSciHubResolver', doi }, () => {
        if (chrome.runtime.lastError) { window.open(link.href, '_blank', 'noopener'); }
      });
    });
    target.insertAdjacentElement('afterend', link);
  }

  addLink();
  new MutationObserver(() => {
    if (scheduled) { return; }
    scheduled = true;
    setTimeout(() => { scheduled = false; addLink(); }, 500);
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
