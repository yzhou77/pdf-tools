/* Shared signature-creator used by the Sign PDF tool and the PDF Edit tool.
 * Opens a modal with three tabs – Draw, Type, Upload – and calls onDone(PNG
 * DataURL, kind) when the user confirms.
 *
 * Usage:
 *   SignatureCreator.open({
 *     title: 'Create signature',
 *     kinds: ['signature', 'initials'],    // optional chip selector
 *     onDone: function(dataUrl, kind) { ... }
 *   });
 */
(function (global) {
    'use strict';

    var modalEl = null;
    var tab = 'draw';
    var drawCanvas = null, drawCtx = null, drawing = false, drawPath = false;
    var typedText = '';
    var typedFont = 'cursive';
    var uploadedDataUrl = null;
    var currentKind = 'signature';
    var onDoneCb = null;

    var TYPE_FONTS = [
        { css: '"Brush Script MT", cursive',            label: 'Brush' },
        { css: '"Segoe Script", "Lucida Handwriting", cursive', label: 'Script' },
        { css: '"Snell Roundhand", cursive',            label: 'Roundhand' },
        { css: '"Courier New", monospace',              label: 'Mono' },
        { css: 'Georgia, serif',                         label: 'Serif' },
    ];

    function el(tagName, attrs, children) {
        var n = document.createElement(tagName);
        if (attrs) {
            for (var k in attrs) if (attrs.hasOwnProperty(k)) {
                if (k === 'class') n.className = attrs[k];
                else if (k === 'style') n.style.cssText = attrs[k];
                else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), attrs[k]);
                else n.setAttribute(k, attrs[k]);
            }
        }
        (children || []).forEach(function (c) {
            if (c == null) return;
            n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
        });
        return n;
    }

    function close() {
        if (modalEl && modalEl.parentNode) modalEl.parentNode.removeChild(modalEl);
        modalEl = null;
    }

    function open(opts) {
        opts = opts || {};
        onDoneCb = opts.onDone;
        currentKind = (opts.kinds && opts.kinds[0]) || 'signature';
        tab = 'draw';
        typedText = opts.initialText || '';
        typedFont = TYPE_FONTS[0].css;
        uploadedDataUrl = null;

        // ---------- tab bar ----------
        var tabBar = el('div', { class: 'sp-tabs' });
        ['draw', 'type', 'upload'].forEach(function (t) {
            var lbl = t === 'draw' ? 'Draw' : t === 'type' ? 'Type' : 'Upload image';
            var b = el('button', {
                type: 'button',
                class: 'btn-action' + (tab === t ? ' active' : ''),
                onclick: function () { tab = t; render(); }
            }, [lbl]);
            b.dataset.tab = t;
            tabBar.appendChild(b);
        });

        // ---------- kind chips (optional) ----------
        var kindsBar = null;
        if (opts.kinds && opts.kinds.length > 1) {
            kindsBar = el('div', { class: 'sp-tabs' });
            opts.kinds.forEach(function (k) {
                var lbl = k === 'initials' ? 'Initials' : (k === 'date' ? 'Date' : 'Signature');
                var b = el('button', {
                    type: 'button',
                    class: 'btn-action' + (currentKind === k ? ' active' : ''),
                    onclick: function () {
                        currentKind = k;
                        render();
                    }
                }, [lbl]);
                kindsBar.appendChild(b);
            });
        }

        var body = el('div');
        var footer = el('div', { class: 'mt-3 d-flex flex-wrap', style: 'gap:6px;' }, [
            el('button', {
                type: 'button',
                class: 'btn btn-danger btn-sm',
                onclick: function () {
                    var url = buildPng();
                    if (!url) return;
                    var k = currentKind;
                    close();
                    if (onDoneCb) onDoneCb(url, k);
                }
            }, ['Insert']),
            el('button', {
                type: 'button',
                class: 'btn btn-secondary btn-sm',
                onclick: close
            }, ['Cancel'])
        ]);

        var dialog = el('div', { class: 'sp-modal-dialog' }, [
            el('h5', { class: 'mb-2' }, [opts.title || 'Create signature']),
            kindsBar,
            tabBar,
            body,
            footer
        ]);

        modalEl = el('div', { class: 'sp-modal' }, [dialog]);
        // Stop outside-click from losing focus in the middle of drawing, but
        // don't eat clicks on the backdrop.
        document.body.appendChild(modalEl);
        renderInto(body);

        function render() {
            // redraw tab buttons' active state without losing state of other tabs
            [].slice.call(tabBar.children).forEach(function (btn) {
                btn.classList.toggle('active', btn.dataset.tab === tab);
            });
            if (kindsBar) {
                [].slice.call(kindsBar.children).forEach(function (btn, i) {
                    btn.classList.toggle('active', opts.kinds[i] === currentKind);
                });
            }
            body.innerHTML = '';
            renderInto(body);
        }

        function renderInto(host) {
            if (tab === 'draw') renderDraw(host);
            else if (tab === 'type') renderType(host, opts);
            else renderUpload(host);
        }

        function renderDraw(host) {
            drawCanvas = el('canvas', { class: 'sp-canvas-draw', width: 600, height: 180 });
            host.appendChild(drawCanvas);
            drawCtx = drawCanvas.getContext('2d');
            drawCtx.lineWidth = 2.5; drawCtx.lineCap = 'round'; drawCtx.lineJoin = 'round';
            drawCtx.strokeStyle = '#111';
            drawing = false; drawPath = false;

            function pt(ev) {
                var r = drawCanvas.getBoundingClientRect();
                return {
                    x: (ev.clientX - r.left) * (drawCanvas.width / r.width),
                    y: (ev.clientY - r.top) * (drawCanvas.height / r.height)
                };
            }
            drawCanvas.addEventListener('pointerdown', function (ev) {
                ev.preventDefault();
                try { drawCanvas.setPointerCapture(ev.pointerId); } catch (e) {}
                drawing = true;
                var p = pt(ev);
                drawCtx.beginPath(); drawCtx.moveTo(p.x, p.y);
            });
            drawCanvas.addEventListener('pointermove', function (ev) {
                if (!drawing) return;
                var p = pt(ev);
                drawCtx.lineTo(p.x, p.y); drawCtx.stroke();
                drawPath = true;
            });
            function endDraw(ev) {
                if (!drawing) return;
                drawing = false;
                try { drawCanvas.releasePointerCapture(ev.pointerId); } catch (e) {}
            }
            drawCanvas.addEventListener('pointerup', endDraw);
            drawCanvas.addEventListener('pointercancel', endDraw);

            host.appendChild(el('div', { class: 'mt-2' }, [
                el('button', {
                    type: 'button',
                    class: 'btn-action',
                    onclick: function () {
                        drawCtx.clearRect(0, 0, drawCanvas.width, drawCanvas.height);
                        drawPath = false;
                    }
                }, ['Clear'])
            ]));
        }

        function renderType(host, opts) {
            var input = el('input', {
                type: 'text',
                class: 'form-control form-control-sm mb-2',
                placeholder: currentKind === 'initials' ? 'e.g. JD' : 'Type your name',
                value: typedText
            });
            input.addEventListener('input', function () { typedText = input.value; preview.textContent = typedText; });
            host.appendChild(input);

            var preview = el('div', {
                class: 'sp-type-preview',
                style: 'font-family: ' + typedFont
            }, [typedText]);
            host.appendChild(preview);

            var fonts = el('div', { class: 'sp-type-fonts' });
            TYPE_FONTS.forEach(function (f) {
                fonts.appendChild(el('button', {
                    type: 'button',
                    class: 'btn-action',
                    style: 'font-family: ' + f.css,
                    onclick: function () {
                        typedFont = f.css;
                        preview.style.fontFamily = f.css;
                    }
                }, [f.label]));
            });
            host.appendChild(fonts);
            setTimeout(function () { input.focus(); }, 0);
        }

        function renderUpload(host) {
            var note = el('div', { class: 'text-muted small mb-2' },
                ['Choose a PNG (transparent background recommended) or JPG file.']);
            host.appendChild(note);

            var fileInput = el('input', { type: 'file', accept: 'image/png,image/jpeg' });
            host.appendChild(fileInput);

            var preview = el('div', { class: 'mt-2', style: 'background:#fff;padding:6px;border:1px solid #888;text-align:center;min-height:80px;' });
            host.appendChild(preview);

            fileInput.addEventListener('change', function () {
                preview.innerHTML = '';
                uploadedDataUrl = null;
                var f = fileInput.files[0];
                if (!f) return;
                if (!/^image\/(png|jpe?g)$/.test(f.type) && !/\.(png|jpe?g)$/i.test(f.name)) {
                    preview.textContent = 'Please pick a PNG or JPG image.';
                    return;
                }
                var reader = new FileReader();
                reader.onload = function (e) {
                    uploadedDataUrl = e.target.result;
                    var img = el('img', { src: uploadedDataUrl, style: 'max-height:90px;max-width:100%;' });
                    preview.appendChild(img);
                };
                reader.readAsDataURL(f);
            });
        }

        function typedToPng() {
            if (!typedText || !typedText.trim()) return null;
            // Render onto a transparent canvas using the chosen font.
            var c = document.createElement('canvas');
            c.width = 800; c.height = 200;
            var g = c.getContext('2d');
            g.clearRect(0, 0, c.width, c.height);
            g.fillStyle = '#111';
            var fontSize = currentKind === 'initials' ? 120 : 78;
            g.font = fontSize + 'px ' + typedFont;
            g.textBaseline = 'middle';
            g.textAlign = 'center';
            g.fillText(typedText, c.width / 2, c.height / 2);
            return trimTransparentPng(c);
        }

        function drawnToPng() {
            if (!drawCanvas || !drawPath) return null;
            return trimTransparentPng(drawCanvas);
        }

        function trimTransparentPng(src) {
            // Crop transparent margins so the final image hugs the strokes.
            var g = src.getContext('2d');
            var w = src.width, h = src.height;
            var data = g.getImageData(0, 0, w, h).data;
            var top = h, bot = -1, left = w, right = -1;
            for (var y = 0; y < h; y++) {
                for (var x = 0; x < w; x++) {
                    if (data[(y * w + x) * 4 + 3] > 4) {
                        if (x < left) left = x;
                        if (x > right) right = x;
                        if (y < top) top = y;
                        if (y > bot) bot = y;
                    }
                }
            }
            if (bot < 0) return null;
            var pad = 4;
            left = Math.max(0, left - pad);
            top = Math.max(0, top - pad);
            right = Math.min(w - 1, right + pad);
            bot = Math.min(h - 1, bot + pad);
            var out = document.createElement('canvas');
            out.width = right - left + 1;
            out.height = bot - top + 1;
            out.getContext('2d').drawImage(src, left, top, out.width, out.height,
                                           0, 0, out.width, out.height);
            return out.toDataURL('image/png');
        }

        function buildPng() {
            if (tab === 'draw')   return drawnToPng() || (alert('Please draw something first.'), null);
            if (tab === 'type')   return typedToPng() || (alert('Please type something first.'), null);
            if (tab === 'upload') return uploadedDataUrl || (alert('Please choose an image first.'), null);
            return null;
        }
    }

    global.SignatureCreator = { open: open };
})(window);