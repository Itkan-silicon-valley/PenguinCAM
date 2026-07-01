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
    sheet: { width: CFG.bed.width || 24, height: CFG.bed.height || 24 },
    parts: [],            // {id,name,width,height,outline,holes,file,place_x,place_y,rotation}
    selectedId: null,
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
      tool: state.tool_diameter, sheet: state.sheet,
      parts: state.parts.map(function (p) {
        return { name: p.name, w: p.width, h: p.height, x: p.place_x, y: p.place_y, rot: p.rotation };
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
    var pts = part.outline.map(function (pt) { return rotatePoint(pt[0], pt[1], part.rotation); });
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    pts.forEach(function (pt) {
      if (pt[0] < minX) minX = pt[0]; if (pt[0] > maxX) maxX = pt[0];
      if (pt[1] < minY) minY = pt[1]; if (pt[1] > maxY) maxY = pt[1];
    });
    var norm = pts.map(function (pt) { return [pt[0] - minX, pt[1] - minY]; });
    var holes = (part.holes || []).map(function (h) {
      var c = rotatePoint(h.cx, h.cy, part.rotation);
      return { cx: c[0] - minX, cy: c[1] - minY, r: h.r };
    });
    return { pts: norm, holes: holes, w: maxX - minX, h: maxY - minY };
  }

  function footprint(part) {
    var s = placedShape(part);
    return { minX: part.place_x, minY: part.place_y, maxX: part.place_x + s.w, maxY: part.place_y + s.h, shape: s };
  }

  // The placed perimeter polygon in sheet coordinates (mirror of placed_polygon()).
  function placedPolygon(part) {
    var s = placedShape(part);
    return s.pts.map(function (pt) { return [part.place_x + pt[0], part.place_y + pt[1]]; });
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

  // Mirror of validate_job_layout: sheet fit via bbox, plus a real-geometry overlap
  // test (so a part nesting into another's concave region isn't a false positive).
  function validateLayout() {
    var msgs = [];
    var bad = {};
    var gap = state.tool_diameter;
    var W = state.sheet.width, H = state.sheet.height;
    var items = state.parts.map(function (p) { return { id: p.id, name: p.name, box: footprint(p), poly: placedPolygon(p) }; });
    items.forEach(function (b) {
      if (b.box.minX < -1e-6 || b.box.minY < -1e-6 || b.box.maxX > W + 1e-6 || b.box.maxY > H + 1e-6) {
        bad[b.id] = true;
        msgs.push(b.name + ' extends outside the sheet.');
      }
    });
    for (var i = 0; i < items.length; i++) {
      for (var j = i + 1; j < items.length; j++) {
        var a = items[i].box, c = items[j].box;
        // Cheap bbox prune: clearly-separated boxes mean clear parts.
        var clearX = (a.maxX + gap <= c.minX + 1e-6) || (c.maxX + gap <= a.minX + 1e-6);
        var clearY = (a.maxY + gap <= c.minY + 1e-6) || (c.maxY + gap <= a.minY + 1e-6);
        if (clearX || clearY) continue;
        // Boxes are close - test the actual perimeters.
        if (polyMinDist(items[i].poly, items[j].poly) < gap - 1e-6) {
          bad[items[i].id] = true; bad[items[j].id] = true;
          msgs.push(items[i].name + ' and ' + items[j].name + ' overlap or are too close.');
        }
      }
    }
    return { bad: bad, msgs: msgs };
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
    if (name === 'layout') { drawLayout(); }
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
      place_x: 0, place_y: 0, rotation: 0,
    };
    // Naive initial placement: stack to the right of existing parts.
    var x = 0;
    state.parts.forEach(function (q) { x = Math.max(x, q.place_x + footprint(q).maxX - q.place_x + state.tool_diameter); });
    p.place_x = state.parts.length ? x : 0;
    state.parts.push(p);
    if (state.selectedId == null) state.selectedId = p.id;
    renderParts();
    dbg('part-added', { name: p.name, w: p.width, h: p.height });
  }

  function removePart(id) {
    state.parts = state.parts.filter(function (p) { return p.id !== id; });
    if (state.selectedId === id) state.selectedId = state.parts.length ? state.parts[0].id : null;
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
  var canvasState = { scale: 1, ox: 0, oy: 0, dragging: null, grab: null };

  function fitTransform(canvas) {
    var W = state.sheet.width, H = state.sheet.height;
    var pad = 16;
    var sc = Math.min((canvas.width - 2 * pad) / W, (canvas.height - 2 * pad) / H);
    canvasState.scale = sc;
    canvasState.ox = pad;
    canvasState.oy = canvas.height - pad; // y flips
  }
  function worldToCanvas(x, y) { return [canvasState.ox + x * canvasState.scale, canvasState.oy - y * canvasState.scale]; }
  function canvasToWorld(cx, cy) { return [(cx - canvasState.ox) / canvasState.scale, (canvasState.oy - cy) / canvasState.scale]; }

  function drawLayout() {
    var canvas = $('#layout-canvas');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    fitTransform(canvas);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Sheet
    var o = worldToCanvas(0, 0), tr = worldToCanvas(state.sheet.width, state.sheet.height);
    ctx.fillStyle = '#11161d';
    ctx.fillRect(tr[0] < o[0] ? tr[0] : o[0], tr[1], Math.abs(tr[0] - o[0]), Math.abs(o[1] - tr[1]));
    ctx.strokeStyle = '#3b4654'; ctx.lineWidth = 1;
    ctx.strokeRect(o[0], tr[1], (tr[0] - o[0]), (o[1] - tr[1]));
    // origin marker
    ctx.fillStyle = '#3fb950'; ctx.beginPath(); ctx.arc(o[0], o[1], 4, 0, 7); ctx.fill();

    var v = validateLayout();
    $('#layout-errors').textContent = v.msgs.join('\n');

    state.parts.forEach(function (p) {
      var s = placedShape(p);
      var invalid = !!v.bad[p.id];
      var selected = p.id === state.selectedId;
      ctx.beginPath();
      s.pts.forEach(function (pt, i) {
        var c = worldToCanvas(p.place_x + pt[0], p.place_y + pt[1]);
        if (i) ctx.lineTo(c[0], c[1]); else ctx.moveTo(c[0], c[1]);
      });
      ctx.closePath();
      ctx.fillStyle = invalid ? 'rgba(248,81,73,0.18)' : (selected ? 'rgba(47,129,247,0.22)' : 'rgba(154,167,180,0.12)');
      ctx.fill();
      ctx.strokeStyle = invalid ? '#f85149' : (selected ? '#2f81f7' : '#9aa7b4');
      ctx.lineWidth = selected ? 2 : 1;
      ctx.stroke();
      s.holes.forEach(function (h) {
        var c = worldToCanvas(p.place_x + h.cx, p.place_y + h.cy);
        ctx.beginPath(); ctx.arc(c[0], c[1], Math.max(1, h.r * canvasState.scale), 0, 7); ctx.strokeStyle = '#9aa7b4'; ctx.stroke();
      });
      // label
      var lc = worldToCanvas(p.place_x, p.place_y + s.h);
      ctx.fillStyle = '#e6edf3'; ctx.font = '11px sans-serif';
      ctx.fillText(p.name, lc[0] + 3, lc[1] + 12);
    });
  }

  function hitTest(wx, wy) {
    // topmost part whose footprint contains the point
    for (var i = state.parts.length - 1; i >= 0; i--) {
      var b = footprint(state.parts[i]);
      if (wx >= b.minX && wx <= b.maxX && wy >= b.minY && wy <= b.maxY) return state.parts[i];
    }
    return null;
  }

  function bindLayout() {
    $('#f-sheet-w').value = state.sheet.width;
    $('#f-sheet-h').value = state.sheet.height;
    $('#f-sheet-w').addEventListener('input', function () { state.sheet.width = parseFloat(this.value) || state.sheet.width; drawLayout(); });
    $('#f-sheet-h').addEventListener('input', function () { state.sheet.height = parseFloat(this.value) || state.sheet.height; drawLayout(); });

    var canvas = $('#layout-canvas');
    function evtWorld(e) {
      var rect = canvas.getBoundingClientRect();
      var t = e.touches ? e.touches[0] : e;
      var cx = (t.clientX - rect.left) * (canvas.width / rect.width);
      var cy = (t.clientY - rect.top) * (canvas.height / rect.height);
      return canvasToWorld(cx, cy);
    }
    function down(e) {
      var w = evtWorld(e);
      var hit = hitTest(w[0], w[1]);
      if (hit) {
        state.selectedId = hit.id;
        canvasState.dragging = hit;
        canvasState.grab = [w[0] - hit.place_x, w[1] - hit.place_y];
        syncRotationControls();
        drawLayout();
        e.preventDefault();
      }
    }
    function move(e) {
      if (!canvasState.dragging) return;
      var w = evtWorld(e);
      var p = canvasState.dragging;
      p.place_x = Math.max(0, w[0] - canvasState.grab[0]);
      p.place_y = Math.max(0, w[1] - canvasState.grab[1]);
      drawLayout();
      e.preventDefault();
    }
    function up() { canvasState.dragging = null; }
    canvas.addEventListener('mousedown', down);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    canvas.addEventListener('touchstart', down, { passive: false });
    canvas.addEventListener('touchmove', move, { passive: false });
    canvas.addEventListener('touchend', up);

    var rot = $('#f-rotation');
    rot.addEventListener('input', function () { setRotation(parseFloat(this.value)); });
    $('#btn-rot-left').addEventListener('click', function () { nudgeRotation(-15); });
    $('#btn-rot-right').addEventListener('click', function () { nudgeRotation(15); });
  }

  function selectedPart() { return state.parts.filter(function (p) { return p.id === state.selectedId; })[0]; }

  function syncRotationControls() {
    var p = selectedPart();
    var has = !!p;
    $('#f-rotation').disabled = !has;
    $('#btn-rot-left').disabled = !has;
    $('#btn-rot-right').disabled = !has;
    if (has) { $('#f-rotation').value = p.rotation; $('#rotation-val').innerHTML = p.rotation.toFixed(0) + '&deg;'; }
  }

  function setRotation(deg) {
    var p = selectedPart(); if (!p) return;
    deg = ((deg % 360) + 360) % 360;
    // Snap within 5 degrees of a 45-degree multiple.
    var snapped = Math.round(deg / 45) * 45;
    if (Math.abs(snapped - deg) <= 5) deg = snapped % 360;
    p.rotation = deg;
    $('#rotation-val').innerHTML = deg.toFixed(0) + '&deg;';
    drawLayout();
  }
  function nudgeRotation(delta) { var p = selectedPart(); if (!p) return; setRotation(p.rotation + delta); $('#f-rotation').value = p.rotation; }

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
    var job = {
      material: state.material, tool_diameter: state.tool_diameter,
      thickness: state.thickness, tab_spacing: state.tab_spacing,
      stock: { width: state.sheet.width, height: state.sheet.height },
      name: 'job', parts: [],
    };
    state.parts.forEach(function (p, i) {
      job.parts.push({ file_index: i, name: p.name, place_x: p.place_x, place_y: p.place_y, rotation: p.rotation });
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
    fd.append('rotation', 0);
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
    var W = (resp.stock && resp.stock.width) || state.sheet.width;
    var D = (resp.stock && resp.stock.height) || state.sheet.height;
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
        if (STEPS[idx + 1] === 'layout') syncRotationControls();
        gotoStep(STEPS[idx + 1]);
      }
    });
    $('#btn-back').addEventListener('click', function () {
      var idx = STEPS.indexOf(state.step);
      if (idx > 0) gotoStep(STEPS[idx - 1]);
    });
  }

  function bindConnect() {
    var btn = $('#btn-connect');
    var poll = null;
    function stopPoll() { if (poll) { clearInterval(poll); poll = null; } }
    function checkAuthed() {
      fetch('/onshape/authed', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          dbg('authed-poll', j);
          if (j && j.authenticated) { stopPoll(); location.reload(); }
        })
        .catch(function (e) { dbg('authed-poll:err', String(e)); });
    }
    if (btn) btn.addEventListener('click', function () {
      $('#connect-status').textContent = 'Opening Onshape sign-in… complete it in the popup.';
      window.open('/onshape/auth?popup=1', 'penguincam_oauth', 'width=520,height=720');
      // Poll for auth completion. Cross-origin OAuth navigation usually severs
      // window.opener, so we don't rely on a postMessage from the popup; the iframe
      // asks the server (via its own cookie) whether tokens have landed yet.
      stopPoll();
      poll = setInterval(checkAuthed, 1500);
      setTimeout(stopPoll, 120000); // give up after 2 min
    });
    // Fast path if the opener relationship happens to survive.
    window.addEventListener('message', function (e) {
      if (e.data === 'penguincam-auth-done') { dbg('auth', 'done-msg'); stopPoll(); location.reload(); }
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
