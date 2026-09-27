import http from './index'

// ----------------------------------------------------------------------
// Saved GEE Assets — durable pointers to Assets the user has explicitly
// chosen to remember (auto-saved by the backend after a successful
// local-CSV → Asset upload; can also be deleted from the UI). The
// pointer is purely local; deleting a row never touches the GEE side.
// ----------------------------------------------------------------------

export function fetchSavedGeeAssets(algorithmId) {
  // Returns [] on any error so the "查看已保存 GEE Assets" checkbox
  // gracefully shows an empty list when the DB is unreachable.
  return http
    .get(`/algorithms/${encodeURIComponent(algorithmId)}/saved-gee-assets`)
    .then((r) => r.data?.assets || [])
    .catch(() => [])
}

export function saveGeeAsset(algorithmId, assetId, sourceFilename = '') {
  return http
    .post(`/algorithms/${encodeURIComponent(algorithmId)}/saved-gee-assets`, {
      asset_id: assetId,
      source_filename: sourceFilename
    })
    .then((r) => r.data)
}

export function deleteSavedGeeAsset(algorithmId, assetId) {
  // The asset_id carries slashes (projects/<p>/assets/<n>); FastAPI's
  // :path matcher handles that on the server side, but axios will
  // percent-encode the slashes correctly via encodeURIComponent here.
  return http
    .delete(
      `/algorithms/${encodeURIComponent(algorithmId)}/saved-gee-assets/${encodeURIComponent(assetId)}`
    )
    .then((r) => r.data)
}

// Best-effort existence / accessibility probe. Used after the user
// pastes an Asset ID into the text input so we can surface "Asset not
// found" inline without waiting 30 minutes for a GEE task to fail.
// Always resolves (never throws) so the form can render the message
// gracefully.
export function verifyGeeAsset(algorithmId, assetId, project = '') {
  return http
    .post(
      `/algorithms/${encodeURIComponent(algorithmId)}/saved-gee-assets/verify`,
      { asset_id: assetId, project: project || null }
    )
    .then((r) => r.data)
    .catch((err) => ({
      ok: false,
      asset_id: assetId,
      error: err?.userMessage || err?.message || '校验失败'
    }))
}