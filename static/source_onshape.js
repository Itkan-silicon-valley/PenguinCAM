/* Onshape "part source" adapter for the wizard (loaded only on /onshape-panel).
 *
 * Flow: announce to the Onshape host (applicationInit) -> on "Select a face",
 * postMessage a requestSelection -> Onshape returns REQUESTED_SELECTION with the
 * chosen face/part IDs -> POST those to /onshape/export-face, which exports a flat
 * DXF server-side and returns its bytes (base64) + outline -> hand the DXF File and
 * outline to the wizard via window.PenguinCAM.onPart. The browser keeps the bytes so
 * the job submit stays self-contained (serverless-safe).
 *
 * Contract with wizard.js:
 *   window.PenguinCAM.requestOnshapeSelection()        - ask Onshape for a face
 *   window.PenguinCAM.onPart(outlineData, dx, File)    - a part is ready to add
 *   window.PenguinCAM.onSelectionBusy(bool)            - export in progress
 *   window.PenguinCAM.onSelectionError(msg)            - something failed
 *   window.PenguinCAM.debug(label, data)               - optional debug logging
 */
(function () {
  'use strict';
  var P = window.PenguinCAM || (window.PenguinCAM = {});
  var ctx = P.onshape || {};
  var counter = 0;

  function dbg(label, data) { if (typeof P.debug === 'function') P.debug(label, data); }
  function post(msg) { try { window.parent.postMessage(msg, '*'); } catch (e) { dbg('onshape:post-fail', String(e)); } }

  function isOnshapeOrigin(origin) {
    var host = (origin || '').replace(/^https?:\/\//, '').split('/')[0] || '';
    return /(^|\.)onshape\.com$/.test(host);
  }

  function init() {
    post({ messageName: 'applicationInit', documentId: ctx.documentId, workspaceId: ctx.workspaceId, elementId: ctx.elementId });
    dbg('onshape:init', ctx);
  }

  P.requestOnshapeSelection = function () {
    counter++;
    post({
      messageName: 'requestSelection',
      messageId: 'penguincam-sel-' + counter,
      documentId: ctx.documentId, workspaceId: ctx.workspaceId, elementId: ctx.elementId,
      filterType: 'simple',
      entityTypeSpecifier: ['FACE'],
      bodyTypeSpecifier: ['SOLID'],
      requiredSelectionCount: 1
    });
    dbg('onshape:requestSelection', counter);
  };

  window.addEventListener('message', function (e) {
    if (!isOnshapeOrigin(e.origin)) return;
    var d = e.data || {};
    if (d.messageName !== 'REQUESTED_SELECTION' && d.messageName !== 'SELECTION') return;
    var sels = d.selections || [];
    var status = (d.status && d.status.statusCode) || (sels.length ? 'SUCCESS' : '');
    dbg('onshape:' + d.messageName, { n: sels.length, status: status });
    if (status === 'PENDING') return;            // user hasn't picked yet
    if (!sels.length) return;                     // deselection / timeout - ignore
    var s = sels[0];
    var faceId = s.selectionId || s.entityId || s.id;
    var partId = s.partId || s.bodyId || null;
    if (!faceId) { if (P.onSelectionError) P.onSelectionError('No face id in selection'); return; }
    exportFace(faceId, partId);
  });

  function exportFace(faceId, partId) {
    if (P.onSelectionBusy) P.onSelectionBusy(true);
    dbg('onshape:export', { faceId: faceId, partId: partId });
    fetch('/onshape/export-face', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        documentId: ctx.documentId, workspaceId: ctx.workspaceId, elementId: ctx.elementId,
        faceId: faceId, partId: partId
      })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (P.onSelectionBusy) P.onSelectionBusy(false);
        if (!res.ok || !res.j.success) {
          if (P.onSelectionError) P.onSelectionError((res.j && res.j.error) || 'export failed');
          return;
        }
        var bin = atob(res.j.dxf);
        var arr = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        var file = new File([arr], (res.j.name || 'part') + '.dxf', { type: 'application/dxf' });
        dbg('onshape:export-ok', { name: res.j.name, w: res.j.width, h: res.j.height });
        if (P.onPart) P.onPart(res.j, file);
      })
      .catch(function (err) {
        if (P.onSelectionBusy) P.onSelectionBusy(false);
        if (P.onSelectionError) P.onSelectionError(String(err));
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
