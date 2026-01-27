import { useState, useRef, useEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { HelpCircle, ExternalLink } from 'lucide-react'

interface InfoTooltipProps {
  text: string
  link?: { url: string; label: string }
  size?: 'sm' | 'md'
  className?: string
}

export default function InfoTooltip({ text, link, size = 'sm', className = '' }: InfoTooltipProps) {
  const [visible, setVisible] = useState(false)
  const [position, setPosition] = useState<{ top: number; left: number; above: boolean }>({
    top: 0,
    left: 0,
    above: true,
  })
  const iconRef = useRef<HTMLSpanElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)

  const iconSize = size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4'

  const updatePosition = useCallback(() => {
    if (!iconRef.current) return
    const rect = iconRef.current.getBoundingClientRect()
    const tooltipHeight = tooltipRef.current?.offsetHeight || 80
    const tooltipWidth = tooltipRef.current?.offsetWidth || 256
    const spaceAbove = rect.top
    const above = spaceAbove > tooltipHeight + 8

    let left = rect.left + rect.width / 2 - tooltipWidth / 2
    // Clamp to viewport
    left = Math.max(8, Math.min(left, window.innerWidth - tooltipWidth - 8))

    setPosition({
      top: above ? rect.top - 8 : rect.bottom + 8,
      left,
      above,
    })
  }, [])

  useEffect(() => {
    if (visible) {
      updatePosition()
      // Recalculate after render so tooltipRef dimensions are available
      requestAnimationFrame(updatePosition)
    }
  }, [visible, updatePosition])

  // Close on click outside (mobile)
  useEffect(() => {
    if (!visible) return
    const handleClickOutside = (e: MouseEvent | TouchEvent) => {
      if (
        iconRef.current &&
        !iconRef.current.contains(e.target as Node) &&
        tooltipRef.current &&
        !tooltipRef.current.contains(e.target as Node)
      ) {
        setVisible(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    document.addEventListener('touchstart', handleClickOutside)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('touchstart', handleClickOutside)
    }
  }, [visible])

  return (
    <>
      <span
        ref={iconRef}
        className={`inline-flex items-center cursor-help ${className}`}
        onMouseEnter={() => setVisible(true)}
        onMouseLeave={() => setVisible(false)}
        onClick={(e) => {
          e.stopPropagation()
          setVisible((v) => !v)
        }}
        role="button"
        aria-label="Information"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && setVisible((v) => !v)}
      >
        <HelpCircle className={`${iconSize} text-gray-400 hover:text-gray-600 transition-colors`} />
      </span>
      {visible &&
        createPortal(
          <div
            ref={tooltipRef}
            className="fixed z-[9999] max-w-xs"
            style={{
              top: position.above ? undefined : `${position.top}px`,
              bottom: position.above ? `${window.innerHeight - position.top}px` : undefined,
              left: `${position.left}px`,
            }}
            onMouseEnter={() => setVisible(true)}
            onMouseLeave={() => setVisible(false)}
          >
            <div className="bg-gray-900 text-white text-xs rounded-lg p-3 shadow-xl transition-opacity duration-150">
              <p className="leading-relaxed">{text}</p>
              {link && (
                <a
                  href={link.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-2 inline-flex items-center gap-1 text-blue-300 hover:text-blue-200"
                  onClick={(e) => e.stopPropagation()}
                >
                  {link.label}
                  <ExternalLink className="w-3 h-3" />
                </a>
              )}
              {/* Arrow */}
              <div
                className={`absolute left-1/2 -translate-x-1/2 w-0 h-0 border-x-[6px] border-x-transparent ${
                  position.above
                    ? 'bottom-[-6px] border-t-[6px] border-t-gray-900'
                    : 'top-[-6px] border-b-[6px] border-b-gray-900'
                }`}
              />
            </div>
          </div>,
          document.body
        )}
    </>
  )
}
