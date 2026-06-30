/* Onshape "part source" adapter for the wizard (loaded only on /onshape-panel).
 *
 * Contract with wizard.js:
 *   - window.PenguinCAM.requestOnshapeSelection()  -> ask Onshape for the selected face
 *   - on a successful export, call window.PenguinCAM.onPart(outlineData, dxfBlob)
 *     where outlineData = { name, width, height, outline, holes } (same shape as
 *     /part-outline returns) and dxfBlob is the exported DXF.
 *
 * Phase C wires this to: Onshape postMessage selection -> POST ids to the server,
 * which exports the face's DXF via onshape_integration and returns the DXF bytes +
 * outline; the bytes are handed to onPart so the job submit stays self-contained.
 *
 * This is currently a placeholder so /onshape-panel loads cleanly; the live
 * implementation is built and verified against the staging Onshape environment.
 */
(function () {
  'use strict';
  var P = window.PenguinCAM || (window.PenguinCAM = {});

  P.requestOnshapeSelection = function () {
    console.warn('[onshape-source] selection requested - adapter not yet implemented (Phase C)');
    if (window.parent !== window) {
      // The eventual flow posts a selection request to the Onshape host.
      try { window.parent.postMessage({ documentId: P.documentId, messageName: 'requestSelection' }, '*'); } catch (e) {}
    }
  };

  // Placeholder listener; the real handler validates event.origin against *.onshape.com,
  // reads the selected face IDs, asks the server to export the DXF, then calls P.onPart.
  window.addEventListener('message', function (e) {
    if (!/\.onshape\.com$/.test((e.origin || '').replace(/^https?:\/\//, '').split('/')[0] || '')) return;
    console.debug('[onshape-source] message from Onshape:', e.data && e.data.messageName);
  });
})();
