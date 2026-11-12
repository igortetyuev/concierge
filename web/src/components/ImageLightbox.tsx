import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { fetchAttachmentObjectUrl } from '../lib/api'
import { IconClose, IconDownload } from './icons'

export interface LightboxImage {
  id: number
  name: string
}

const ZOOM_PRESETS = [0.5, 1, 2]
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

interface Props {
  images: LightboxImage[]
  initialIndex: number
  /** Object URL already loaded for `images[initialIndex]` — stays owned by the caller. */
  initialUrl: string
  onClose: () => void
}

export default function ImageLightbox({ images, initialIndex, initialUrl, onClose }: Props) {
  const [index, setIndex] = useState(initialIndex)
  const [zoom, setZoom] = useState<'fit' | number>('fit')
  const [nat, setNat] = useState({ w: 0, h: 0 })
  const [urls, setUrls] = useState<ReadonlyMap<number, string>>(
    () => new Map([[images[initialIndex].id, initialUrl]]),
  )
  const viewRef = useRef<HTMLDivElement>(null)

  const image = images[index]
  const url = urls.get(image.id) ?? ''

  useEffect(() => {
    if (urls.has(image.id)) return
    let alive = true
    void fetchAttachmentObjectUrl(image.id)
      .then((u) => {
        if (!alive) {
          URL.revokeObjectURL(u)
          return
        }
        setUrls((m) => new Map(m).set(image.id, u))
      })
      .catch(() => {
        /* the view just keeps showing the spinner */
      })
    return () => {
      alive = false
    }
  }, [image.id, urls])

  useEffect(() => {
    return () => {
      for (const u of urls.values()) {
        if (u !== initialUrl) URL.revokeObjectURL(u)
      }
    }
  }, [urls, initialUrl])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    const el = viewRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      setZoom((z) => {
        const cur =
          z === 'fit'
            ? nat.w
              ? Math.min((window.innerWidth - 48) / nat.w, (window.innerHeight - 140) / nat.h)
              : 1
            : z
        return clamp(Math.round(cur * (e.deltaY < 0 ? 1.2 : 1 / 1.2) * 100) / 100, 0.05, 8)
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [nat])

  function selectImage(i: number) {
    setIndex(i)
    setNat({ w: 0, h: 0 })
    viewRef.current?.scrollTo(0, 0)
  }

  function download() {
    if (!url) return
    const a = document.createElement('a')
    a.href = url
    a.download = image.name
    a.click()
  }

  const zoomValue = zoom === 'fit' ? 'fit' : String(zoom)

  return createPortal(
    <div className="lb">
      <div
        className="lb-view"
        ref={viewRef}
        onClick={(e) => {
          if ((e.target as HTMLElement).tagName !== 'IMG') onClose()
        }}
      >
        <div className="lb-frame">
          {url ? (
            <img
              src={url}
              alt={image.name}
              draggable={false}
              onLoad={(e) =>
                setNat({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
              }
              onDoubleClick={() => setZoom(zoom === 'fit' ? 1 : 'fit')}
              style={
                zoom === 'fit' || !nat.w
                  ? { maxWidth: 'calc(100vw - 48px)', maxHeight: 'calc(var(--vvh) - 140px)' }
                  : { width: nat.w * zoom, maxWidth: 'none', maxHeight: 'none' }
              }
            />
          ) : (
            <span className="lb-loading">загрузка…</span>
          )}
        </div>
      </div>
      <div className="lb-bar">
        {images.length > 1 ? (
          <select
            className="lb-media"
            value={index}
            onChange={(e) => selectImage(Number(e.target.value))}
            aria-label="Вложение"
          >
            {images.map((im, i) => (
              <option key={im.id} value={i}>
                {im.name}
              </option>
            ))}
          </select>
        ) : (
          <span className="lb-name">{image.name}</span>
        )}
        <select
          className="lb-zoom"
          value={zoomValue}
          onChange={(e) => setZoom(e.target.value === 'fit' ? 'fit' : Number(e.target.value))}
          aria-label="Масштаб"
        >
          <option value="fit">Fit</option>
          {ZOOM_PRESETS.map((z) => (
            <option key={z} value={z}>
              {z * 100}%
            </option>
          ))}
          {zoom !== 'fit' && !ZOOM_PRESETS.includes(zoom) && (
            <option value={zoom}>{Math.round(zoom * 100)}%</option>
          )}
        </select>
        <button className="lb-btn" onClick={download} aria-label="Скачать" title="Скачать">
          <IconDownload size={18} />
        </button>
        <button className="lb-btn" onClick={onClose} aria-label="Закрыть" title="Закрыть">
          <IconClose size={18} />
        </button>
      </div>
    </div>,
    document.body,
  )
}
