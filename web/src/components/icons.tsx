interface IconProps {
  size?: number
  className?: string
}

function svg(path: string, extra = ''): (props: IconProps) => React.JSX.Element {
  return function Icon({ size = 20, className }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        aria-hidden="true"
      >
        <path d={path} />
        {extra && <path d={extra} />}
      </svg>
    )
  }
}

export const IconChat = svg('M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z')
export const IconUsers = svg(
  'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2',
  'M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
)
export const IconPlus = svg('M12 5v14M5 12h14')
export const IconSearch = svg('M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.35-4.35')
export const IconSend = svg('M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z')
export const IconBack = svg('M15 18l-6-6 6-6')
export const IconInfo = svg(
  'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z',
  'M12 16v-4M12 8h.01',
)
export const IconArchive = svg(
  'M21 8v13H3V8',
  'M1 3h22v5H1zM10 12h4',
)
export const IconLogout = svg(
  'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4',
  'M16 17l5-5-5-5M21 12H9',
)
export const IconClose = svg('M18 6L6 18M6 6l12 12')
export const IconCheck = svg('M20 6L9 17l-5-5')
export const IconCheckAll = svg('M18 6L7 17l-5-5M22 10l-7.5 7.5L13 16')
export const IconDown = svg('M12 5v14M5 12l7 7 7-7')
export const IconEdit = svg(
  'M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z',
)
export const IconRetry = svg('M23 4v6h-6M1 20v-6h6', 'M3.5 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.5 15')
export const IconTrash = svg(
  'M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6',
)
export const IconTool = svg(
  'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z',
)
export const IconPerson = svg('M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2', 'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z')
export const IconSettings = svg(
  'M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z',
  'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
)
export const IconBook = svg(
  'M4 19.5A2.5 2.5 0 0 1 6.5 17H20',
  'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z',
)
export const IconPaperclip = svg(
  'M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48',
)
export const IconCamera = svg(
  'M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z',
  'M12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
)
export const IconMic = svg(
  'M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z',
  'M19 10v2a7 7 0 0 1-14 0v-2M12 19v4',
)
export const IconSpeaker = svg(
  'M11 5L6 9H2v6h4l5 4V5z',
  'M15.54 8.46a5 5 0 0 1 0 7.07M19.07 4.93a10 10 0 0 1 0 14.14',
)
export const IconStop = svg('M6 6h12v12H6z')
export const IconCopy = svg(
  'M11 9h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2z',
  'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1',
)
export const IconDownload = svg(
  'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4',
  'M7 10l5 5 5-5M12 15V3',
)
export const IconFile = svg(
  'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z',
  'M14 2v6h6',
)
