/* PenguinCAM multi-part wizard.
 * One codebase for the standalone (/app, source=upload) and embedded
 * (/onshape-panel, source=onshape) contexts; the part source is the only
 * difference and is selected via window.PenguinCAM.source.
 */
(function () {
  'use strict';

  var CFG = window.PenguinCAM || { source: 'upload', bed: { width: 24, height: 24 }, defaultTool: 0.157 };
  var DEBUG = /(?:^|[?&])debug=1(?:&|$)/.test(location.search);

  var STEPS = ['setup', 'parts', 'layout', 'preview'];

  var state = {
    source: CFG.source,
    step: 'setup',
    mode: '2d',
    machine_id: null,
    material: 'plywood',
    tool_diameter: parseFloat(CFG.defaultTool) || 0.157,
    thickness: 0.25,
    tab_spacing: 6.0,
    // The machine envelope is a read-only constraint; the parts' combined bounding box
    // is the stock (G54 origin = its lower-left).
    machine: { width: CFG.bed.width || 24, height: CFG.bed.height || 24, name: CFG.machineName || 'Machine' },
    parts: [],            // {id,name,width,height,outline,holes,file,cx,cy,rotation,flipped}
    selectedIds: [],
    zoom: 1,
    lastResponse: null,
  };
  var partSeq = 0;
  var debugEvents = [];
  var viewer = null;

  /* ----------------------------------------------------------------- utils */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function dbg(label, data) {
    debugEvents.unshift({ t: new Date().toISOString().slice(11, 19), label: label, data: data });
    debugEvents = debugEvents.slice(0, 12);
    renderDebug();
  }

  function renderDebug() {
    if (!DEBUG) return;
    var el = $('#debug-content');
    if (!el) return;
    var snapshot = {
      source: state.source, step: state.step, mode: state.mode,
      tool: state.tool_diameter, machine: state.machine,
      parts: state.parts.map(function (p) {
        return { name: p.name, w: p.width, h: p.height, cx: p.cx, cy: p.cy, rot: p.rotation };
      }),
    };
    el.textContent = JSON.stringify(snapshot, null, 1) + '\n--- events ---\n' +
      debugEvents.map(function (e) { return e.t + ' ' + e.label + ' ' + (e.data != null ? JSON.stringify(e.data) : ''); }).join('\n');
  }

  function timestamp() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* -------------------------------------------------------------- geometry */
  function rotatePoint(x, y, deg) {
    // Match the server: transform_coordinates rotates clockwise (angle_rad = -radians).
    var a = -deg * Math.PI / 180;
    return [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
  }

  // Returns the part's placed footprint: outline points normalized so the rotated
  // bounding-box minimum is (0,0), plus that footprint's width/height. Mirrors the
  // server pinning the rotated bbox-min to placement_offset.
  function placedShape(part) {
    var fx = part.flipped ? -1 : 1;   // horizontal flip (mirror across X) before rotating
    var pts = part.outline.map(function (pt) { return rotatePoint(fx * pt[0], pt[1], part.rotation); });
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    pts.forEach(function (pt) {
      if (pt[0] < minX) minX = pt[0]; if (pt[0] > maxX) maxX = pt[0];
      if (pt[1] < minY) minY = pt[1]; if (pt[1] > maxY) maxY = pt[1];
    });
    var norm = pts.map(function (pt) { return [pt[0] - minX, pt[1] - minY]; });
    var holes = (part.holes || []).map(function (h) {
      var c = rotatePoint(fx * h.cx, h.cy, part.rotation);
      return { cx: c[0] - minX, cy: c[1] - minY, r: h.r };
    });
    return { pts: norm, holes: holes, w: maxX - minX, h: maxY - minY };
  }

  // Parts are stored by their center (cx, cy) so rotation happens in place. The
  // placement is the derived axis-aligned footprint whose bbox-min the server pins to
  // place_x/place_y.
  function placement(part) {
    var s = placedShape(part);
    return { x: part.cx - s.w / 2, y: part.cy - s.h / 2, w: s.w, h: s.h, shape: s };
  }

  function footprint(part) {
    var p = placement(part);
    return { minX: p.x, minY: p.y, maxX: p.x + p.w, maxY: p.y + p.h, shape: p.shape };
  }

  // The placed perimeter polygon in sheet coordinates (mirror of placed_polygon()).
  function placedPolygon(part) {
    var p = placement(part);
    return p.shape.pts.map(function (pt) { return [p.x + pt[0], p.y + pt[1]]; });
  }

  function segPointDist(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    if (len2 === 0) return Math.hypot(px - ax, py - ay);
    var t = ((px - ax) * dx + (py - ay) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }
  function segsIntersect(a, b, c, d) {
    function ccw(p, q, r) { return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0]); }
    return ccw(a, c, d) !== ccw(b, c, d) && ccw(a, b, c) !== ccw(a, b, d);
  }
  function pointInPoly(pt, poly) {
    var x = pt[0], y = pt[1], inside = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  // Minimum distance between two polygon outlines (0 if they intersect or one
  // contains the other). Used for the kerf-gap proximity test.
  function polyMinDist(A, B) {
    if (pointInPoly(A[0], B) || pointInPoly(B[0], A)) return 0;
    var min = Infinity;
    for (var i = 0; i < A.length; i++) {
      var a1 = A[i], a2 = A[(i + 1) % A.length];
      for (var j = 0; j < B.length; j++) {
        var b1 = B[j], b2 = B[(j + 1) % B.length];
        if (segsIntersect(a1, a2, b1, b2)) return 0;
        var d = Math.min(
          segPointDist(a1[0], a1[1], b1[0], b1[1], b2[0], b2[1]),
          segPointDist(a2[0], a2[1], b1[0], b1[1], b2[0], b2[1]),
          segPointDist(b1[0], b1[1], a1[0], a1[1], a2[0], a2[1]),
          segPointDist(b2[0], b2[1], a1[0], a1[1], a2[0], a2[1])
        );
        if (d < min) min = d;
      }
    }
    return min;
  }

  // Combined footprint of a set of parts (all parts by default). This is the stock;
  // its lower-left is the G54 origin. Returns null when empty.
  function combinedBBox(parts) {
    parts = parts || state.parts;
    if (!parts.length) return null;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    parts.forEach(function (p) {
      var f = footprint(p);
      if (f.minX < minX) minX = f.minX; if (f.minY < minY) minY = f.minY;
      if (f.maxX > maxX) maxX = f.maxX; if (f.maxY > maxY) maxY = f.maxY;
    });
    return { minX: minX, minY: minY, maxX: maxX, maxY: maxY, w: maxX - minX, h: maxY - minY };
  }

  function isSelected(id) { return state.selectedIds.indexOf(id) >= 0; }
  function selectedParts() { return state.parts.filter(function (p) { return isSelected(p.id); }); }

  // Validate the layout: the combined bounding box (the stock) must fit the machine,
  // and no two parts may overlap or sit closer than one kerf. Real-geometry overlap so
  // a part nesting into another's concave region isn't a false positive.
  function validateLayout() {
    var msgs = [];
    var bad = {};
    var gap = state.tool_diameter;
    var bbox = combinedBBox();
    var tooBig = false;
    if (bbox && (bbox.w > state.machine.width + 1e-6 || bbox.h > state.machine.height + 1e-6)) {
      tooBig = true;
      msgs.push('Parts (' + bbox.w.toFixed(2) + '" x ' + bbox.h.toFixed(2) + '") exceed the machine (' +
                state.machine.width + '" x ' + state.machine.height + '").');
    }
    var items = state.parts.map(function (p) { return { id: p.id, name: p.name, box: footprint(p), poly: placedPolygon(p) }; });
    for (var i = 0; i < items.length; i++) {
      for (var j = i + 1; j < items.length; j++) {
        var a = items[i].box, c = items[j].box;
        var clearX = (a.maxX + gap <= c.minX + 1e-6) || (c.maxX + gap <= a.minX + 1e-6);
        var clearY = (a.maxY + gap <= c.minY + 1e-6) || (c.maxY + gap <= a.minY + 1e-6);
        if (clearX || clearY) continue;
        if (polyMinDist(items[i].poly, items[j].poly) < gap - 1e-6) {
          bad[items[i].id] = true; bad[items[j].id] = true;
          msgs.push(items[i].name + ' and ' + items[j].name + ' overlap or are too close.');
        }
      }
    }
    return { bad: bad, msgs: msgs, tooBig: tooBig, bbox: bbox };
  }

  /* ------------------------------------------------------------ step nav */
  function gotoStep(name) {
    state.step = name;
    $all('.step').forEach(function (s) { s.hidden = s.getAttribute('data-step') !== name; });
    $all('#stepbar li').forEach(function (li) {
      var s = li.getAttribute('data-step');
      li.classList.toggle('active', s === name);
      li.classList.toggle('done', STEPS.indexOf(s) < STEPS.indexOf(name));
    });
    var idx = STEPS.indexOf(name);
    $('#btn-back').disabled = idx === 0;
    var nextBtn = $('#btn-next');
    nextBtn.hidden = name === 'preview';
    if (name === 'layout') { updateLayoutInfo(); resetHandleDir(); refitView(); drawLayout(); }
    if (name === 'preview') { resetPreview(); }
    dbg('step', name);
  }

  function canLeave(name) {
    if (name === 'parts' && state.parts.length === 0) {
      alert('Add at least one part before continuing.');
      return false;
    }
    if (name === 'layout') {
      var v = validateLayout();
      if (v.msgs.length) {
        alert('Fix the layout first:\n' + v.msgs.join('\n'));
        return false;
      }
    }
    return true;
  }

  // Jump to a step via the stepbar. Backward is always allowed; forward must clear the
  // same gates as pressing Next through each intervening step.
  function navigateTo(name) {
    var target = STEPS.indexOf(name), cur = STEPS.indexOf(state.step);
    if (target < 0 || target === cur) return;
    if (target > cur) {
      for (var i = cur; i < target; i++) { if (!canLeave(STEPS[i])) return; }
    }
    gotoStep(name);
  }

  /* --------------------------------------------------------------- setup */
  function bindSetup() {
    var machineSel = $('#f-machine');
    if (machineSel) { state.machine_id = machineSel.value; machineSel.addEventListener('change', function () { state.machine_id = this.value; }); }

    $all('input[name="mode"]').forEach(function (r) {
      r.addEventListener('change', function () {
        state.mode = this.value;
        var is25 = state.mode === '2.5d';
        $('#thickness-field').style.display = is25 ? 'none' : '';
        $('#thickness-derived').style.display = is25 ? '' : 'none';
        updatePartsModeNote();
        dbg('mode', state.mode);
      });
    });

    $('#f-tool').addEventListener('input', function () { state.tool_diameter = parseFloat(this.value) || state.tool_diameter; });
    $('#f-material').addEventListener('change', function () { state.material = this.value; });
    $('#f-thickness').addEventListener('input', function () { state.thickness = parseFloat(this.value) || state.thickness; });
  }

  function updatePartsModeNote() {
    var note = $('#parts-mode-note');
    if (state.mode === '2.5d') {
      note.textContent = '2.5D mode: one part per job (thickness comes from the CAD layers).';
    } else {
      note.textContent = 'Add as many parts as fit on the sheet.';
    }
  }

  /* --------------------------------------------------------------- parts */
  function thumbnailSVG(part) {
    var W = 44, H = 44, pad = 4;
    var scale = Math.min((W - 2 * pad) / (part.width || 1), (H - 2 * pad) / (part.height || 1));
    function map(x, y) { return [pad + x * scale, H - pad - y * scale]; }
    var d = part.outline.map(function (pt, i) { var m = map(pt[0], pt[1]); return (i ? 'L' : 'M') + m[0].toFixed(1) + ' ' + m[1].toFixed(1); }).join(' ') + ' Z';
    var holes = (part.holes || []).map(function (h) { var m = map(h.cx, h.cy); return '<circle cx="' + m[0].toFixed(1) + '" cy="' + m[1].toFixed(1) + '" r="' + Math.max(1, h.r * scale).toFixed(1) + '" fill="none" stroke="#9aa7b4"/>'; }).join('');
    return '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '"><path d="' + d + '" fill="none" stroke="#2f81f7" stroke-width="1.5"/>' + holes + '</svg>';
  }

  function renderParts() {
    var ul = $('#parts-list');
    ul.innerHTML = '';
    state.parts.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'part-item';
      li.innerHTML = thumbnailSVG(p) +
        '<div class="meta"><div class="name"></div><div class="dims">' +
        p.width.toFixed(2) + '" x ' + p.height.toFixed(2) + '"</div></div>' +
        '<button class="remove" title="Remove" aria-label="Remove">&times;</button>';
      li.querySelector('.name').textContent = p.name;
      li.querySelector('.remove').addEventListener('click', function () { removePart(p.id); });
      ul.appendChild(li);
    });
    renderDebug();
  }

  function addPartFromOutline(data, file) {
    if (state.mode === '2.5d' && state.parts.length >= 1) {
      alert('2.5D mode allows only one part. Remove the current part first, or switch to 2D mode.');
      return;
    }
    var p = {
      id: ++partSeq,
      name: data.name || ('part ' + (partSeq)),
      width: data.width, height: data.height,
      outline: data.outline, holes: data.holes || [],
      file: file,
      cx: 0, cy: 0, rotation: 0, flipped: false,
    };
    // Initial placement: bottom edge on Y=0, stacked to the right of existing parts.
    var s = placedShape(p);
    var startX = 0;
    state.parts.forEach(function (q) { startX = Math.max(startX, footprint(q).maxX + state.tool_diameter); });
    p.cx = startX + s.w / 2;
    p.cy = s.h / 2;
    state.parts.push(p);
    renderParts();
    dbg('part-added', { name: p.name, w: p.width, h: p.height });
  }

  function removePart(id) {
    state.parts = state.parts.filter(function (p) { return p.id !== id; });
    state.selectedIds = state.selectedIds.filter(function (sid) { return sid !== id; });
    renderParts();
    if (state.step === 'layout') drawLayout();
  }

  function uploadDxf(file) {
    if (!file || !/\.dxf$/i.test(file.name)) { alert('Please choose a .dxf file.'); return; }
    var fd = new FormData();
    fd.append('file', file);
    dbg('part-outline:req', file.name);
    fetch('/part-outline', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.success) { dbg('part-outline:err', res.j.error); alert('Could not read DXF: ' + (res.j.error || 'unknown error')); return; }
        dbg('part-outline:ok', { name: res.j.name, w: res.j.width, h: res.j.height });
        addPartFromOutline(res.j, file);
      })
      .catch(function (e) { dbg('part-outline:fail', String(e)); alert('Upload failed: ' + e); });
  }

  function bindParts() {
    if (state.source === 'onshape') {
      $('#upload-source').hidden = true;
      $('#onshape-source').hidden = false;
      var sel = $('#btn-select-face');
      // The Onshape adapter calls this when a face has been exported to a DXF blob.
      window.PenguinCAM.onPart = function (data, fileOrBlob) {
        var file = fileOrBlob instanceof File ? fileOrBlob : new File([fileOrBlob], (data.name || 'part') + '.dxf');
        addPartFromOutline(data, file);
      };
      window.PenguinCAM.onSelectionBusy = function (busy) {
        if (!sel) return;
        sel.disabled = busy;
        sel.textContent = busy ? 'Exporting selected face…' : 'Select a face in Onshape';
      };
      window.PenguinCAM.onSelectionError = function (msg) {
        dbg('onshape:error', msg);
        if (sel) { sel.disabled = false; sel.textContent = 'Select a face in Onshape'; }
        alert('Onshape selection failed: ' + msg);
      };
      if (sel) sel.addEventListener('click', function () {
        if (window.PenguinCAM.requestOnshapeSelection) window.PenguinCAM.requestOnshapeSelection();
        else dbg('onshape', 'adapter not loaded');
      });
    } else {
      var dz = $('#dropzone'), input = $('#f-dxf');
      dz.addEventListener('click', function () { input.click(); });
      dz.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') input.click(); });
      input.addEventListener('change', function () { if (this.files[0]) uploadDxf(this.files[0]); this.value = ''; });
      ['dragover', 'dragenter'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); }); });
      ['dragleave', 'drop'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); }); });
      dz.addEventListener('drop', function (e) { if (e.dataTransfer.files[0]) uploadDxf(e.dataTransfer.files[0]); });
    }
    updatePartsModeNote();
  }

  /* -------------------------------------------------------------- layout */
  var canvasState = { scale: 1, wcx: 0, wcy: 0, ccx: 0, ccy: 0, action: null, handleDir: [0, 1] };

  // Fit the parts' combined bounding box to ~80% of the canvas (times zoom), centered.
  // Called only on explicit events (entering Layout, zoom) — NOT every frame, so the
  // view stays put while dragging/rotating and part motion is actually visible.
  function refitView() {
    var canvas = $('#layout-canvas');
    if (!canvas) return;
    var bb = combinedBBox();
    var w = bb ? Math.max(bb.w, 0.001) : 10;
    var h = bb ? Math.max(bb.h, 0.001) : 10;
    canvasState.wcx = bb ? (bb.minX + bb.maxX) / 2 : 0;
    canvasState.wcy = bb ? (bb.minY + bb.maxY) / 2 : 0;
    canvasState.ccx = canvas.width / 2;
    canvasState.ccy = canvas.height / 2;
    canvasState.scale = Math.min(canvas.width / w, canvas.height / h) * 0.8 * state.zoom;
  }
  function worldToCanvas(x, y) {
    return [canvasState.ccx + (x - canvasState.wcx) * canvasState.scale,
            canvasState.ccy - (y - canvasState.wcy) * canvasState.scale];
  }
  function canvasToWorld(cx, cy) {
    return [canvasState.wcx + (cx - canvasState.ccx) / canvasState.scale,
            canvasState.wcy - (cy - canvasState.ccy) / canvasState.scale];
  }

  // The rotation handle orbits the selection center along canvasState.handleDir (a
  // world-space unit vector that follows the pointer while rotating and persists after).
  // The bounding box stays axis-aligned; only the handle moves around it.
  function selectionHandle(selBox) {
    var cxw = (selBox.minX + selBox.maxX) / 2, cyw = (selBox.minY + selBox.maxY) / 2;
    var ctr = worldToCanvas(cxw, cyw);
    var dir = canvasState.handleDir || [0, 1];
    var dp = worldToCanvas(cxw + dir[0], cyw + dir[1]);
    var ux = dp[0] - ctr[0], uy = dp[1] - ctr[1], ul = Math.hypot(ux, uy) || 1; ux /= ul; uy /= ul;
    var a = worldToCanvas(selBox.minX, selBox.minY), b = worldToCanvas(selBox.maxX, selBox.maxY);
    var hw = Math.abs(b[0] - a[0]) / 2, hh = Math.abs(b[1] - a[1]) / 2;
    // Distance from center to the box edge along the handle direction (ray-rectangle
    // hit), so the stem meets the box exactly instead of a circumscribed circle.
    var tEdge = Math.min(
      Math.abs(ux) > 1e-6 ? hw / Math.abs(ux) : Infinity,
      Math.abs(uy) > 1e-6 ? hh / Math.abs(uy) : Infinity
    );
    if (!isFinite(tEdge)) tEdge = 0;
    return { ex: ctr[0] + ux * tEdge, ey: ctr[1] + uy * tEdge, hx: ctr[0] + ux * (tEdge + 26), hy: ctr[1] + uy * (tEdge + 26) };
  }

  // Point the handle sensibly when the selection changes: along a single part's "up",
  // or north for a group.
  function resetHandleDir() {
    var sel = selectedParts();
    canvasState.handleDir = (sel.length === 1) ? rotatePoint(0, 1, sel[0].rotation) : [0, 1];
  }

  function drawLayout() {
    var canvas = $('#layout-canvas');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    var v = validateLayout();
    $('#layout-errors').textContent = v.msgs.join('\n');
    var flipBtn = $('#btn-flip'); if (flipBtn) flipBtn.disabled = state.selectedIds.length === 0;

    // Stock = combined bounding box (dotted). Red if it exceeds the machine. The G54
    // origin marker sits at its lower-left.
    var bb = v.bbox;
    if (bb) {
      var a = worldToCanvas(bb.minX, bb.minY), c = worldToCanvas(bb.maxX, bb.maxY);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = v.tooBig ? '#f85149' : '#5b6876'; ctx.lineWidth = 1;
      ctx.strokeRect(Math.min(a[0], c[0]), Math.min(a[1], c[1]), Math.abs(c[0] - a[0]), Math.abs(c[1] - a[1]));
      ctx.setLineDash([]);
      ctx.fillStyle = '#3fb950'; ctx.beginPath(); ctx.arc(a[0], a[1], 4, 0, 7); ctx.fill();
      ctx.restore();
    }

    // Parts.
    state.parts.forEach(function (p) {
      var pl = placement(p), s = pl.shape;
      var invalid = !!v.bad[p.id];
      var selected = isSelected(p.id);
      ctx.beginPath();
      s.pts.forEach(function (pt, i) {
        var pc = worldToCanvas(pl.x + pt[0], pl.y + pt[1]);
        if (i) ctx.lineTo(pc[0], pc[1]); else ctx.moveTo(pc[0], pc[1]);
      });
      ctx.closePath();
      ctx.fillStyle = invalid ? 'rgba(248,81,73,0.18)' : (selected ? 'rgba(47,129,247,0.22)' : 'rgba(154,167,180,0.12)');
      ctx.fill();
      ctx.strokeStyle = invalid ? '#f85149' : (selected ? '#2f81f7' : '#9aa7b4');
      ctx.lineWidth = selected ? 2 : 1;
      ctx.stroke();
      s.holes.forEach(function (h) {
        var hc = worldToCanvas(pl.x + h.cx, pl.y + h.cy);
        ctx.beginPath(); ctx.arc(hc[0], hc[1], Math.max(1, h.r * canvasState.scale), 0, 7); ctx.strokeStyle = '#9aa7b4'; ctx.stroke();
      });
      var lc = worldToCanvas(pl.x, pl.y + pl.h);
      ctx.fillStyle = '#e6edf3'; ctx.font = '11px sans-serif';
      ctx.fillText(p.name + (p.flipped ? ' (flipped)' : ''), lc[0] + 3, lc[1] + 12);
    });

    // Selection box + rotation handle.
    var selBox = combinedBBox(selectedParts());
    if (selBox) {
      var a2 = worldToCanvas(selBox.minX, selBox.minY), b2 = worldToCanvas(selBox.maxX, selBox.maxY);
      ctx.save();
      ctx.strokeStyle = '#2f81f7'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.strokeRect(Math.min(a2[0], b2[0]), Math.min(a2[1], b2[1]), Math.abs(b2[0] - a2[0]), Math.abs(b2[1] - a2[1]));
      ctx.setLineDash([]);
      var hg = selectionHandle(selBox);
      ctx.beginPath(); ctx.moveTo(hg.ex, hg.ey); ctx.lineTo(hg.hx, hg.hy);
      ctx.strokeStyle = '#2f81f7'; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.beginPath(); ctx.arc(hg.hx, hg.hy, 6, 0, 7); ctx.fillStyle = '#2f81f7'; ctx.fill();
      ctx.restore();
    }

    // Combined size readout, upper-right.
    ctx.save();
    ctx.textAlign = 'right'; ctx.font = '12px sans-serif';
    ctx.fillStyle = v.tooBig ? '#f85149' : '#9aa7b4';
    ctx.fillText(bb ? (bb.w.toFixed(2) + '" x ' + bb.h.toFixed(2) + '"') : 'no parts', canvas.width - 8, 16);
    ctx.restore();
  }

  function hitTest(wx, wy) {
    for (var i = state.parts.length - 1; i >= 0; i--) {
      var b = footprint(state.parts[i]);
      if (wx >= b.minX && wx <= b.maxX && wy >= b.minY && wy <= b.maxY) return state.parts[i];
    }
    return null;
  }

  function bindLayout() {
    var canvas = $('#layout-canvas');
    function evtCanvas(e) {
      var rect = canvas.getBoundingClientRect();
      var t = e.touches ? e.touches[0] : e;
      return [(t.clientX - rect.left) * (canvas.width / rect.width),
              (t.clientY - rect.top) * (canvas.height / rect.height)];
    }
    function down(e) {
      var c = evtCanvas(e), w = canvasToWorld(c[0], c[1]);
      var shift = e.shiftKey;
      var selBox = combinedBBox(selectedParts());
      // Rotation handle rotates the whole selection about its center.
      if (selBox) {
        var hg = selectionHandle(selBox);
        if (Math.hypot(c[0] - hg.hx, c[1] - hg.hy) <= 12) {
          var pivot = [(selBox.minX + selBox.maxX) / 2, (selBox.minY + selBox.maxY) / 2];
          canvasState.action = {
            type: 'rotate', pivot: pivot,
            refAngle: Math.atan2(w[1] - pivot[1], w[0] - pivot[0]),
            snap: selectedParts().map(function (p) { return { p: p, cx: p.cx, cy: p.cy, rot: p.rotation }; })
          };
          e.preventDefault();
          return;
        }
      }
      var hit = hitTest(w[0], w[1]);
      if (hit) {
        if (shift) {
          if (isSelected(hit.id)) state.selectedIds = state.selectedIds.filter(function (id) { return id !== hit.id; });
          else state.selectedIds.push(hit.id);
          resetHandleDir();
        } else {
          if (!isSelected(hit.id)) { state.selectedIds = [hit.id]; resetHandleDir(); }
          canvasState.action = {
            type: 'drag', startWorld: w,
            snap: selectedParts().map(function (p) { return { p: p, cx: p.cx, cy: p.cy }; })
          };
        }
        drawLayout();
        e.preventDefault();
      } else if (!shift) {
        state.selectedIds = [];  // click empty space to deselect
        drawLayout();
      }
    }
    function move(e) {
      var act = canvasState.action;
      if (!act) return;
      var c = evtCanvas(e), w = canvasToWorld(c[0], c[1]);
      if (act.type === 'drag') {
        var dx = w[0] - act.startWorld[0], dy = w[1] - act.startWorld[1];
        act.snap.forEach(function (s) { s.p.cx = s.cx + dx; s.p.cy = s.cy + dy; });
      } else if (act.type === 'rotate') {
        var cur = Math.atan2(w[1] - act.pivot[1], w[0] - act.pivot[0]);
        // Handle follows the pointer around the box (and persists after release).
        var hl = Math.hypot(w[0] - act.pivot[0], w[1] - act.pivot[1]) || 1;
        canvasState.handleDir = [(w[0] - act.pivot[0]) / hl, (w[1] - act.pivot[1]) / hl];
        var cwDeg = -(cur - act.refAngle) * 180 / Math.PI;  // clockwise-positive delta
        var snapped = Math.round(cwDeg / 45) * 45;
        if (Math.abs(snapped - cwDeg) <= 5) cwDeg = snapped;
        act.snap.forEach(function (s) {
          var vv = rotatePoint(s.cx - act.pivot[0], s.cy - act.pivot[1], cwDeg);
          s.p.cx = act.pivot[0] + vv[0];
          s.p.cy = act.pivot[1] + vv[1];
          s.p.rotation = (((s.rot + cwDeg) % 360) + 360) % 360;
        });
      }
      drawLayout();
      e.preventDefault();
    }
    function up() { canvasState.action = null; }
    canvas.addEventListener('mousedown', down);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    canvas.addEventListener('touchstart', down, { passive: false });
    canvas.addEventListener('touchmove', move, { passive: false });
    canvas.addEventListener('touchend', up);

    $('#btn-flip').addEventListener('click', function () {
      selectedParts().forEach(function (p) { p.flipped = !p.flipped; });
      drawLayout();
    });
    $('#btn-zoom-in').addEventListener('click', function () { state.zoom = Math.min(5, state.zoom * 1.25); refitView(); drawLayout(); });
    $('#btn-zoom-out').addEventListener('click', function () { state.zoom = Math.max(0.2, state.zoom / 1.25); refitView(); drawLayout(); });
  }

  function updateLayoutInfo() {
    var el = $('#info-machine-name'); if (el) el.textContent = state.machine.name;
    el = $('#info-machine-size'); if (el) el.textContent = state.machine.width + '" x ' + state.machine.height + '"';
    el = $('#info-tool'); if (el) el.textContent = (+state.tool_diameter).toFixed(3) + '"';
  }

  /* ------------------------------------------------------------- preview */
  function resetPreview() {
    $('#preview-result').hidden = true;
    $('#preview-errors').textContent = '';
    $('#gen-status').textContent = '';
  }

  function bindPreview() { $('#btn-generate').addEventListener('click', generate); }

  function generate() {
    $('#preview-errors').textContent = '';
    $('#gen-status').textContent = 'Generating...';
    $('#btn-generate').disabled = true;
    var done = function () { $('#btn-generate').disabled = false; };

    if (state.mode === '2.5d') { generateSingle().then(done, done); }
    else { generateJob().then(done, done); }
  }

  function generateJob() {
    var fd = new FormData();
    // The parts' combined bounding box is the stock; its lower-left is the G54 origin,
    // so placements are normalized relative to it.
    var bb = combinedBBox() || { minX: 0, minY: 0, w: 0, h: 0 };
    var job = {
      material: state.material, tool_diameter: state.tool_diameter,
      thickness: state.thickness, tab_spacing: state.tab_spacing,
      stock: { width: bb.w, height: bb.h },
      name: 'job', parts: [],
    };
    state.parts.forEach(function (p, i) {
      var pl = placement(p);
      job.parts.push({
        file_index: i, name: p.name,
        place_x: pl.x - bb.minX, place_y: pl.y - bb.minY,
        rotation: p.rotation, mirror: !!p.flipped,
      });
      fd.append('file_' + i, p.file, p.name + '.dxf');
    });
    fd.append('job', JSON.stringify(job));
    fd.append('timestamp', timestamp());
    dbg('process-job:req', { parts: job.parts.length });
    return fetch('/process-job', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.success) { showGenErrors(res.j); return; }
        dbg('process-job:ok', { parts: (res.j.parts || []).length, time: res.j.cycle_time });
        state.lastResponse = res.j;
        showResult(res.j);
      })
      .catch(function (e) { dbg('process-job:fail', String(e)); $('#preview-errors').textContent = 'Request failed: ' + e; $('#gen-status').textContent = ''; });
  }

  function generateSingle() {
    var p = state.parts[0];
    if (!p) { $('#preview-errors').textContent = 'Add a part first.'; $('#gen-status').textContent = ''; return Promise.resolve(); }
    var fd = new FormData();
    fd.append('file', p.file, p.name + '.dxf');
    fd.append('material', state.material);
    fd.append('tool_diameter', state.tool_diameter);
    fd.append('thickness', state.thickness);
    fd.append('origin_corner', 'bottom-left');
    fd.append('rotation', Math.round(p.rotation) % 360);
    fd.append('mirror', p.flipped ? '1' : '0');
    fd.append('tab_spacing', state.tab_spacing);
    fd.append('timestamp', timestamp());
    fd.append('suggested_filename', p.name);
    dbg('process:req', p.name);
    return fetch('/process', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || res.j.error) { $('#preview-errors').textContent = res.j.details || res.j.error || 'Generation failed'; $('#gen-status').textContent = ''; return; }
        dbg('process:ok', { time: res.j.cycle_time });
        state.lastResponse = res.j;
        showResult(res.j);
      })
      .catch(function (e) { $('#preview-errors').textContent = 'Request failed: ' + e; $('#gen-status').textContent = ''; });
  }

  function showGenErrors(j) {
    $('#gen-status').textContent = '';
    if (j && j.part_errors) {
      $('#preview-errors').textContent = j.part_errors.map(function (e) { return '• ' + e.error; }).join('\n');
    } else {
      $('#preview-errors').textContent = (j && (j.error || j.details)) || 'Generation failed';
    }
  }

  function showResult(resp) {
    $('#gen-status').textContent = '';
    $('#preview-result').hidden = false;
    var t = resp.cycle_time ? ('Estimated cycle time: ' + resp.cycle_time) : '';
    var n = resp.parts ? (resp.parts.length + ' part(s)') : '1 part';
    $('#preview-stats').textContent = [n, t].filter(Boolean).join(' · ');
    $('#download-link').href = '/download/' + resp.filename;
    show3DPreview(resp);
  }

  function show3DPreview(resp) {
    if (typeof THREE === 'undefined' || typeof GcodeViewer === 'undefined') {
      $('#viewer-empty').textContent = '3D preview unavailable (three.js failed to load).';
      return;
    }
    if (!viewer) {
      viewer = new GcodeViewer({
        canvas: $('#gcode-canvas'), container: $('#viewer-container'),
        scrubber: $('#toolpath-scrubber'), scrubberContainer: $('#scrubber-container'),
        scrubberLabel: $('#scrubber-label'), scrubberOp: $('#scrubber-op'),
        playbackControls: $('#playback-controls'), playButton: $('#play-button'),
        restartButton: $('#restart-button'), speedSelect: $('#playback-speed'),
        resetButton: $('#reset-view'), emptyState: $('#viewer-empty'),
      });
    }
    var bb = combinedBBox();
    var W = (resp.stock && resp.stock.width) || (bb ? bb.w : state.machine.width);
    var D = (resp.stock && resp.stock.height) || (bb ? bb.h : state.machine.height);
    viewer.load(resp.gcode, {
      stockWidth: W, stockDepth: D,
      stockHeight: state.thickness, toolDiameter: state.tool_diameter,
    });
    dbg('preview', { w: W, d: D });
  }

  /* ----------------------------------------------------------------- init */
  function bindNav() {
    $('#btn-next').addEventListener('click', function () {
      var idx = STEPS.indexOf(state.step);
      if (idx < STEPS.length - 1 && canLeave(state.step)) {
        gotoStep(STEPS[idx + 1]);
      }
    });
    $('#btn-back').addEventListener('click', function () {
      var idx = STEPS.indexOf(state.step);
      if (idx > 0) gotoStep(STEPS[idx - 1]);
    });

    // Clickable stepper pills (delegated), keyboard-activatable.
    var bar = $('#stepbar');
    function pillActivate(e) {
      var li = e.target.closest ? e.target.closest('li[data-step]') : null;
      if (!li) return;
      if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      navigateTo(li.getAttribute('data-step'));
    }
    if (bar) { bar.addEventListener('click', pillActivate); bar.addEventListener('keydown', pillActivate); }
  }

  function bindConnect() {
    var btn = $('#btn-connect');
    if (!btn) return;
    var watching = false;
    function setStatus(msg) { $('#connect-status').textContent = msg; }

    // Confirm the iframe's own session is authenticated, then reload once to re-render
    // with the now-authenticated server context (config banner, material/tool options).
    function verifyAndEnter() {
      fetch('/onshape/authed', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          dbg('authed-check', j);
          if (j && j.authenticated) location.reload();
          else { watching = false; setStatus('Sign-in didn’t complete. Click Connect to try again.'); }
        })
        .catch(function (e) { watching = false; dbg('authed-check:err', String(e)); setStatus('Could not verify sign-in — try again.'); });
    }

    btn.addEventListener('click', function () {
      if (watching) return;
      var popup = window.open('/onshape/auth?popup=1', 'penguincam_oauth', 'width=520,height=720');
      if (!popup) { setStatus('Popup blocked — allow popups for this site, then click Connect.'); return; }
      watching = true;
      setStatus('Complete sign-in in the popup window…');
      // Watch the popup close locally (no server polling); cross-origin OAuth navigation
      // severs window.opener, so a postMessage from the popup isn't reliable. When it
      // closes, verify auth once. A safety cap stops the watcher if the popup lingers.
      var iv = setInterval(function () {
        if (popup.closed) { clearInterval(iv); setStatus('Finishing sign-in…'); verifyAndEnter(); }
      }, 500);
      setTimeout(function () { clearInterval(iv); }, 180000);
    });
  }

  function init() {
    if (DEBUG) { $('#debug-overlay').hidden = false; }
    window.PenguinCAM.debug = dbg; // let the Onshape adapter log into the debug overlay
    bindSetup();
    bindParts();
    bindLayout();
    bindPreview();
    bindNav();
    bindConnect();
    gotoStep('setup');
    dbg('init', { source: state.source, authed: CFG.authenticated });
    if (state.source === 'onshape' && !CFG.authenticated) {
      $('#connect-overlay').hidden = false;
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
