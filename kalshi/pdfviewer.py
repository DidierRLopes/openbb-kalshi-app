"""Shared in-page PDF viewer: buttons that open a backend-proxied document in a
modal, rendered to <canvas> with PDF.js (no child frame, so it survives the
widget sandbox) with fit-to-width and zoom. Self-contained — it carries its own
<style>, modal markup, and script, and reuses the host page's theme variables
(--bg/--text/--muted/--line/--accent)."""

from __future__ import annotations

from html import escape
from typing import Any

_DOC_STYLE = """<style>
.doc-links { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.doc-links button { border: 1px solid var(--line); border-radius: 8px; padding: 8px 16px;
  background: none; color: var(--text); font: inherit; font-weight: 600; cursor: pointer; }
.doc-links button:hover { border-color: var(--accent); }
.doc-modal { position: fixed; inset: 0; z-index: 1000; }
.doc-modal[hidden] { display: none; }
.doc-bg { position: absolute; inset: 0; background: rgba(0, 0, 0, 0.6); }
.doc-card { position: absolute; inset: 4% 5%; display: flex; flex-direction: column;
  background: var(--bg); border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45); }
.doc-head { display: flex; align-items: center; gap: 14px; padding: 10px 16px;
  border-bottom: 1px solid var(--line); font-size: 13px; font-weight: 650; }
.doc-head > span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-zoom { display: flex; align-items: center; gap: 6px; }
.doc-zoom button { background: none; border: 1px solid var(--line); border-radius: 6px; color: var(--text);
  height: 24px; min-width: 26px; padding: 0 7px; cursor: pointer; font-size: 13px; line-height: 1; }
.doc-zoom #doc-zlevel { font-size: 12px; color: var(--muted); min-width: 42px; text-align: center; }
.doc-open { font-size: 12px; font-weight: 500; color: var(--accent); white-space: nowrap; }
.doc-x { background: none; border: 0; color: var(--muted); font-size: 16px; cursor: pointer; line-height: 1; }
.doc-frame { flex: 1; width: 100%; overflow: auto; background: #525659; padding: 16px; }
.doc-page { display: block; margin: 0 auto 12px; max-width: 100%; height: auto;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.5); }
.doc-msg { color: #fff; text-align: center; padding: 40px 16px; font-size: 13px; }
</style>"""

_DOC_MODAL = (
    '<div id="doc-modal" class="doc-modal" hidden>'
    '<div class="doc-bg" data-close></div>'
    '<div class="doc-card">'
    '<div class="doc-head"><span id="doc-title"></span>'
    '<div class="doc-zoom">'
    '<button type="button" id="doc-zout" aria-label="Zoom out">−</button>'
    '<span id="doc-zlevel">100%</span>'
    '<button type="button" id="doc-zin" aria-label="Zoom in">+</button>'
    '<button type="button" id="doc-zfit">Fit</button></div>'
    '<a id="doc-open" href="#" target="_blank" rel="noopener">Open in new tab ↗</a>'
    '<button type="button" class="doc-x" data-close aria-label="Close">✕</button></div>'
    '<div id="doc-pages" class="doc-frame"></div>'
    '</div></div>'
)

_DOC_SCRIPT = """<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script>
(function () {
  if (window.pdfjsLib) {
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
  }
  var m = document.getElementById('doc-modal'), pages = document.getElementById('doc-pages'),
      t = document.getElementById('doc-title'), o = document.getElementById('doc-open'),
      zlevel = document.getElementById('doc-zlevel');
  var pdfDoc = null, scale = 1.5;
  function msg(s) { pages.innerHTML = '<div class="doc-msg">' + s + '</div>'; }
  function hide() { m.hidden = true; pages.innerHTML = ''; pdfDoc = null; }
  function renderAll() {
    if (!pdfDoc) return;
    zlevel.textContent = Math.round(scale * 100) + '%';
    pages.innerHTML = '';
    (function page(i) {
      if (i > pdfDoc.numPages) return;
      pdfDoc.getPage(i).then(function (pg) {
        var vp = pg.getViewport({ scale: scale });
        var c = document.createElement('canvas');
        c.className = 'doc-page'; c.width = vp.width; c.height = vp.height;
        pages.appendChild(c);
        pg.render({ canvasContext: c.getContext('2d'), viewport: vp }).promise.then(function () { page(i + 1); });
      });
    })(1);
  }
  function fitWidth() {
    if (!pdfDoc) return;
    pdfDoc.getPage(1).then(function (pg) {
      var w = pg.getViewport({ scale: 1 }).width, avail = pages.clientWidth - 32;
      scale = (avail > 0 && w > 0) ? avail / w : 1.5;
      renderAll();
    });
  }
  function zoom(f) { scale = Math.max(0.25, Math.min(6, scale * f)); renderAll(); }
  function show(u, l) {
    t.textContent = l; o.href = u; m.hidden = false; msg('Loading...');
    if (!window.pdfjsLib) { msg('Use "Open in new tab".'); return; }
    fetch(u).then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) { return pdfjsLib.getDocument({ data: buf }).promise; })
      .then(function (pdf) { pdfDoc = pdf; fitWidth(); })
      .catch(function () { msg('Could not load the document. Use "Open in new tab".'); });
  }
  document.querySelectorAll('[data-doc]').forEach(function (b) {
    b.addEventListener('click', function () {
      show(b.getAttribute('data-doc'), b.getAttribute('data-label'));
    });
  });
  document.querySelectorAll('[data-close]').forEach(function (b) { b.addEventListener('click', hide); });
  document.getElementById('doc-zin').addEventListener('click', function () { zoom(1.25); });
  document.getElementById('doc-zout').addEventListener('click', function () { zoom(0.8); });
  document.getElementById('doc-zfit').addEventListener('click', fitWidth);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
})();
</script>"""


def doc_section(series: dict[str, Any], doc_base: str) -> str:
    """Buttons + modal for a series' rules / contract-terms PDFs. `doc_base` is the
    backend document URL up to (but not including) `&doc=`. Empty if no docs."""
    buttons = []
    for label, key, doc in (
        ("Full rules", "contract_url", "rules"),
        ("Contract terms", "contract_terms_url", "terms"),
    ):
        if doc_base and series.get(key):
            url = escape(f"{doc_base}&doc={doc}")
            buttons.append(f'<button type="button" data-doc="{url}" data-label="{escape(label)}">{label}</button>')
    if not buttons:
        return ""
    return f'<div class="doc-links">{"".join(buttons)}</div>{_DOC_STYLE}{_DOC_MODAL}{_DOC_SCRIPT}'
