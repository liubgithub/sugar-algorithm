import http from './index'

// Upload one or more files into an algorithm/slot.
//
// `files` is always a File[] (or single File wrapped in an array). For the
// algorithm-aware layout pass `algorithm` and `slot`; for slots that declare
// `group_by`, also pass `groupValue` (e.g. the target_month text). The
// server returns the absolute on-disk path which the caller should assign
// to its form param.
export function uploadAsset({ algorithm, slot, files, groupValue, label, onProgress }) {
  const list = Array.isArray(files) ? files : [files]
  const form = new FormData()
  if (list.length === 1) {
    form.append('file', list[0])
  } else {
    for (const f of list) form.append('files', f)
  }
  if (algorithm) form.append('algorithm', algorithm)
  if (slot) form.append('slot', slot)
  if (groupValue) form.append('group_value', groupValue)
  if (label) form.append('label', label)
  return http
    .post('/uploads', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (evt) => {
        if (onProgress && evt.total) {
          onProgress(Math.round((evt.loaded * 100) / evt.total))
        }
      }
    })
    .then((r) => r.data)
}