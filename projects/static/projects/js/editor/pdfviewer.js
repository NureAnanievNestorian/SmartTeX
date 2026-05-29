import * as pdfjsLib from "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs";
import * as state from "./state.js";
import * as apiMod from "./api.js";
import * as cm from "./cm.js";

const { s, cfg } = state;
const { api } = apiMod;
const { jumpToLine } = cm;

pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.mjs";

const pdfCanvasContainer = document.getElementById("pdf-canvas-container");
const pdfEmpty           = document.getElementById("pdf-empty");
const pdfLoadingEl       = document.getElementById("pdf-loading");
const pdfPageInfo        = document.getElementById("pdf-page-info");

export { pdfEmpty };

export async function renderPdfPages(sizeOnly = false) {
  if (!s.pdfDoc || s.pdfRendering) return;
  s.pdfRendering = true;
  const dpr = window.devicePixelRatio || 1;
  const containerW = pdfCanvasContainer.clientWidth - 16;
  const savedScroll = pdfCanvasContainer.scrollTop;
  s.pdfViewports = [];

  if (sizeOnly) {
    for (let i = 1; i <= s.pdfDoc.numPages; i++) {
      const page = await s.pdfDoc.getPage(i);
      const base = page.getViewport({ scale: 1 });
      const scale = Math.max(0.5, containerW / base.width);
      const vp   = page.getViewport({ scale });
      const vpHD = page.getViewport({ scale: scale * dpr });
      s.pdfViewports.push(vp);
      const wrap = pdfCanvasContainer.children[i - 1];
      if (!wrap) break;
      const canvas = wrap.querySelector("canvas");
      if (!canvas) break;
      wrap.dataset.scale = scale;
      wrap.dataset.baseH = base.height;
      canvas.width  = Math.round(vpHD.width);
      canvas.height = Math.round(vpHD.height);
      canvas.style.width  = Math.round(vp.width)  + "px";
      canvas.style.height = Math.round(vp.height) + "px";
      wrap.style.width  = Math.round(vp.width)  + "px";
      wrap.style.height = Math.round(vp.height) + "px";
      await page.render({ canvasContext: canvas.getContext("2d"), viewport: vpHD }).promise;
    }
    s.pdfRendering = false;
    return;
  }

  const fragment = document.createDocumentFragment();
  for (let i = 1; i <= s.pdfDoc.numPages; i++) {
    const page = await s.pdfDoc.getPage(i);
    const base = page.getViewport({ scale: 1 });
    const scale = Math.max(0.5, containerW / base.width);
    const vp   = page.getViewport({ scale });
    const vpHD = page.getViewport({ scale: scale * dpr });
    s.pdfViewports.push(vp);

    const wrap = document.createElement("div");
    wrap.className = "pdf-page-wrap";
    wrap.dataset.page = i;
    wrap.dataset.scale = scale;
    wrap.dataset.baseH = base.height;
    wrap.style.width  = Math.round(vp.width)  + "px";
    wrap.style.height = Math.round(vp.height) + "px";

    const canvas = document.createElement("canvas");
    canvas.width  = Math.round(vpHD.width);
    canvas.height = Math.round(vpHD.height);
    canvas.style.width  = Math.round(vp.width)  + "px";
    canvas.style.height = Math.round(vp.height) + "px";
    wrap.appendChild(canvas);

    canvas.addEventListener("click", async (e) => {
      if (!s.supportsSynctex) return;
      const curScale = parseFloat(wrap.dataset.scale);
      const curBaseH = parseFloat(wrap.dataset.baseH);
      const rect = canvas.getBoundingClientRect();
      const pdfX = (e.clientX - rect.left) / curScale;
      const pdfY = curBaseH - (e.clientY - rect.top) / curScale;
      try {
        const r = await api(`/api/projects/${cfg.projectId}/synctex/pdf/?page=${i}&x=${pdfX.toFixed(4)}&y=${pdfY.toFixed(4)}`);
        if (r.line) {
          jumpToLine(r.line);
          const marker = document.createElement("div");
          marker.className = "pdf-synctex-marker";
          marker.style.left = (e.clientX - rect.left) + "px";
          marker.style.top  = (e.clientY - rect.top) + "px";
          wrap.appendChild(marker);
          setTimeout(() => marker.remove(), 1500);
        }
      } catch (_) {}
    });

    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vpHD }).promise;
    fragment.appendChild(wrap);
  }

  pdfCanvasContainer.replaceChildren(fragment);
  pdfCanvasContainer.scrollTop = savedScroll;
  s.pdfRendering = false;
}

export async function loadPdfViewer(url) {
  const savedScroll = pdfCanvasContainer.scrollTop;
  const isFirstLoad = s.pdfDoc === null;
  pdfLoadingEl.style.display = "flex";
  pdfEmpty.style.display = "none";
  try {
    const loadingTask = pdfjsLib.getDocument(url);
    s.pdfDoc = await loadingTask.promise;
    s.pdfCurrentUrl = url;
    pdfLoadingEl.style.display = "none";
    pdfPageInfo.textContent = `${s.pdfDoc.numPages} стор.`;
    s.pdfRendering = false;
    await renderPdfPages();
    if (!isFirstLoad) pdfCanvasContainer.scrollTop = savedScroll;
  } catch (_) {
    pdfLoadingEl.style.display = "none";
    pdfEmpty.style.display = "flex";
  }
}

// Update page indicator on scroll
pdfCanvasContainer.addEventListener("scroll", () => {
  if (!s.pdfDoc) return;
  const scrollY = pdfCanvasContainer.scrollTop + pdfCanvasContainer.clientHeight / 2;
  let cumH = 0;
  for (let i = 0; i < s.pdfViewports.length; i++) {
    cumH += (s.pdfViewports[i]?.height || 0) + 12;
    if (scrollY <= cumH) {
      pdfPageInfo.textContent = `${i + 1} / ${s.pdfDoc.numPages}`;
      break;
    }
  }
});

// Re-render on resize
const pdfResizeObserver = new ResizeObserver(() => {
  if (s.pdfDoc && !s.pdfRendering) renderPdfPages(true);
});
pdfResizeObserver.observe(pdfCanvasContainer);
