/* Shared helpers used by Merge / Split / Remove / Rotate / Watermark / Encrypt pages.
 * Requires pdf.js to be loaded on the page when you call loadPdfFromFile(). */
(function (global) {
    'use strict';
    function setupPdfJs() {
        if (global.pdfjsLib && !global.pdfjsLib.GlobalWorkerOptions.workerSrc) {
            global.pdfjsLib.GlobalWorkerOptions.workerSrc =
                'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
        }
    }
    function loadPdfFromFile(file) {
        setupPdfJs();
        return new Promise(function (resolve, reject) {
            if (!global.pdfjsLib) { reject(new Error('pdf.js not loaded')); return; }
            var reader = new FileReader();
            reader.onload = function (ev) {
                global.pdfjsLib.getDocument({ data: new Uint8Array(ev.target.result) })
                    .promise.then(resolve, reject);
            };
            reader.onerror = function () { reject(reader.error); };
            reader.readAsArrayBuffer(file);
        });
    }
    function renderPageToCanvas(pdfDoc, pageNum, canvas, scale) {
        return pdfDoc.getPage(pageNum).then(function (page) {
            var viewport = page.getViewport({ scale: scale || 0.25 });
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            return page.render({
                canvasContext: canvas.getContext('2d'),
                viewport: viewport,
            }).promise;
        });
    }
    function compressPages(pages) {
        var arr = Array.from(new Set(pages)).filter(function (n) {
            return Number.isInteger(n) && n > 0;
        }).sort(function (a, b) { return a - b; });
        if (arr.length === 0) return '';
        var out = [], start = arr[0], prev = arr[0];
        for (var i = 1; i < arr.length; i++) {
            if (arr[i] === prev + 1) { prev = arr[i]; continue; }
            out.push(start === prev ? String(start) : start + '-' + prev);
            start = arr[i]; prev = arr[i];
        }
        out.push(start === prev ? String(start) : start + '-' + prev);
        return out.join(',');
    }
    function expandRanges(text, totalPages) {
        var set = new Set();
        if (!text) return set;
        var tokens = text.split(',');
        for (var i = 0; i < tokens.length; i++) {
            var t = tokens[i].trim();
            if (!t) continue;
            var m;
            if ((m = /^(\d+)\s*-\s*(\d+)$/.exec(t))) {
                var a = parseInt(m[1], 10), b = parseInt(m[2], 10);
                if (isNaN(a) || isNaN(b) || a < 1 || b < 1 || a > b) continue;
                for (var n = a; n <= b; n++) {
                    if (!totalPages || n <= totalPages) set.add(n);
                }
            } else if (/^\d+$/.test(t)) {
                var v = parseInt(t, 10);
                if (v >= 1 && (!totalPages || v <= totalPages)) set.add(v);
            }
        }
        return set;
    }
    function contiguousRuns(pages) {
        var arr = Array.from(new Set(pages)).filter(function (n) {
            return Number.isInteger(n) && n > 0;
        }).sort(function (a, b) { return a - b; });
        var runs = [];
        if (arr.length === 0) return runs;
        var start = arr[0], prev = arr[0];
        for (var i = 1; i < arr.length; i++) {
            if (arr[i] === prev + 1) { prev = arr[i]; continue; }
            runs.push([start, prev]);
            start = arr[i]; prev = arr[i];
        }
        runs.push([start, prev]);
        return runs;
    }
    function buildThumbGrid(pdfDoc, gridEl, onToggle) {
        gridEl.innerHTML = '';
        var cells = {};
        var total = pdfDoc.numPages;
        for (var p = 1; p <= total; p++) {
            (function (pageNum) {
                var cell = document.createElement('div');
                cell.className = 'thumb-cell';
                cell.dataset.page = pageNum;
                var canvas = document.createElement('canvas');
                cell.appendChild(canvas);
                var label = document.createElement('span');
                label.className = 'page-label';
                label.textContent = 'Page ' + pageNum;
                cell.appendChild(label);
                cell.addEventListener('click', function () {
                    var nowSelected = !cell.classList.contains('selected');
                    cell.classList.toggle('selected', nowSelected);
                    if (typeof onToggle === 'function') onToggle(pageNum, nowSelected);
                });
                gridEl.appendChild(cell);
                cells[pageNum] = cell;
                renderPageToCanvas(pdfDoc, pageNum, canvas, 0.2).catch(function () {});
            })(p);
        }
        return cells;
    }
    function setGridSelection(cells, pagesSet) {
        Object.keys(cells).forEach(function (k) {
            var n = parseInt(k, 10);
            cells[n].classList.toggle('selected', pagesSet.has(n));
        });
    }
        // Send a file to a tool's ``action=prepare`` endpoint, which converts
    // non-PDF uploads server-side, then hand back both the PDF Blob and a
    // pdf.js document ready for rendering.
    //
    // Every viewer-based screen (crop/edit/organize/ocr/summarize/compare/
    // form/sign/redact) needed exactly this, so it lived as nine identical
    // copies. One copy now.
    function prepareViaServer(url, file, fieldName) {
        setupPdfJs();
        var fd = new FormData();
        fd.append('action', 'prepare');
        fd.append(fieldName || 'file', file, file.name);
        return fetch(url, { method: 'POST', body: fd })
            .then(function (r) {
                if (r.ok) return r.blob();
                // Error responses are JSON; fall back to a status message if
                // the body is not parseable (e.g. a proxy error page).
                return r.json().then(function (j) {
                    throw new Error(j && j.error ? j.error : 'Preparation failed.');
                }, function () {
                    throw new Error('Preparation failed (HTTP ' + r.status + ').');
                });
            })
            .then(function (blob) {
                return blob.arrayBuffer().then(function (buf) {
                    return global.pdfjsLib.getDocument({ data: new Uint8Array(buf) })
                        .promise.then(function (doc) {
                            return { blob: blob, doc: doc };
                        });
                });
            });
    }
    global.PDFHelpers = {
        setupPdfJs: setupPdfJs,
        loadPdfFromFile: loadPdfFromFile,
        renderPageToCanvas: renderPageToCanvas,
        compressPages: compressPages,
        expandRanges: expandRanges,
        contiguousRuns: contiguousRuns,
        buildThumbGrid: buildThumbGrid,
        setGridSelection: setGridSelection,
        prepareViaServer: prepareViaServer
    };
})(window);

